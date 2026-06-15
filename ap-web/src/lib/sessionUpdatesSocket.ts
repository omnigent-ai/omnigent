// Persistent WebSocket client for `WS /v1/sessions/updates`.
//
// Replaces the sidebar's 4 s HTTP poll of `GET /v1/sessions` with a single
// long-lived connection: the client tells the server which session ids it's
// displaying (the "watch-set"), and the server pushes a snapshot followed by
// diffs whenever those sessions change. See the server endpoint docstring in
// `omnigent/server/routes/sessions.py` for the wire protocol.
//
// This module owns only the transport (connect, reconnect, send watch-set,
// dispatch frames). SessionUpdatesProvider wires the parsed frames into the
// TanStack Query cache and derives the watch-set from it.
//
// Identity rides the transport exactly like the terminal-attach WebSocket:
// the browser cannot set `X-Forwarded-Email` on a WebSocket handshake, so we
// rely on the ingress / dev proxy to carry the authenticated identity. The
// id list is NEVER trusted from the client for authorization — the server
// access-checks every watched id against the connection's user.

import { resolveWebSocketUrl } from "@/lib/host";
import type { SessionListWireItem } from "@/lib/sessionListCache";

/** A frame pushed by the server over the updates stream. */
export type SessionUpdatesFrame =
  | { type: "snapshot"; items: SessionListWireItem[] }
  | { type: "changed"; items: SessionListWireItem[] }
  | { type: "removed"; ids: string[] }
  | { type: "heartbeat" };

type FrameListener = (frame: SessionUpdatesFrame) => void;

// Reconnect backoff — mirrors the chat stream pump (chatStore.ts): 250 ms
// base, doubling, capped at 5 s, with ±50% jitter so many tabs reconnecting
// after a server blip don't synchronize into a thundering herd.
const RECONNECT_BASE_MS = 250;
const RECONNECT_MAX_MS = 5_000;

// The server pushes a heartbeat every 30 s when a watch-set is idle (see
// `_SESSION_UPDATES_HEARTBEAT_INTERVAL_S`). If we go appreciably longer than
// that with no frame of any kind, the connection is silently dead — a
// half-open TCP socket or an idle-reaping proxy that never sent a close. The
// browser's `onclose` may not fire for a long time in that case, which would
// strand the sidebar (push is dead, and the HTTP fallback poll stays suspended
// because we still believe we're connected). This watchdog forces a reconnect
// so liveness is restored. Set to a little over 2× the heartbeat so a single
// late/dropped heartbeat doesn't churn the connection, but two missed in a row
// do. Exported so the watchdog test can advance fake timers to the exact
// threshold rather than hard-coding the literal.
export const HEARTBEAT_WATCHDOG_MS = 70_000;

function nextReconnectDelay(failedAttempts: number): number {
  const base = Math.min(RECONNECT_BASE_MS * 2 ** (failedAttempts - 1), RECONNECT_MAX_MS);
  return base / 2 + Math.random() * (base / 2);
}

/**
 * Build the `ws(s)://` URL for the session-updates endpoint.
 *
 * Delegates to the host seam (`resolveWebSocketUrl`), exactly like the
 * terminal-attach socket: standalone builds the URL from the page origin
 * (whether served by the Omnigent server directly or through the Vite dev proxy),
 * and an embedding host rebases it onto its proxied WS surface.
 *
 * @returns The fully-qualified WebSocket URL.
 */
function buildUpdatesUrl(): string {
  return resolveWebSocketUrl("/v1/sessions/updates");
}

/**
 * Singleton transport for the session-updates WebSocket. One instance per
 * tab; {@link SessionUpdatesProvider} starts it once at app mount.
 */
class SessionUpdatesSocket {
  private ws: WebSocket | null = null;
  private watched: string[] = [];
  private watchedKey = "";
  private readonly listeners = new Set<FrameListener>();
  private readonly statusListeners = new Set<() => void>();
  private connected = false;
  private failedAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private watchdogTimer: ReturnType<typeof setTimeout> | null = null;
  private started = false;

  /** Open the connection (idempotent). */
  start(): void {
    if (this.started) return;
    this.started = true;
    this.connect();
  }

  /** Close the connection and stop reconnecting. */
  stop(): void {
    this.started = false;
    this.clearWatchdog();
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      // Drop handlers first so the close doesn't schedule a reconnect.
      ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
      ws.close();
    }
    this.setConnected(false);
  }

  /**
   * Whether the stream is currently open. Consumers use this to tune their
   * HTTP fallback cadence: a live stream keeps watched rows fresh by push,
   * but list-level discovery still needs occasional reconciliation.
   *
   * @returns `true` when the WebSocket is open.
   */
  isConnected(): boolean {
    return this.connected;
  }

  /**
   * Subscribe to connection-state changes (for ``useSyncExternalStore``).
   *
   * @param listener - Called whenever {@link isConnected} flips.
   * @returns An unsubscribe function.
   */
  subscribeStatus(listener: () => void): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  private setConnected(value: boolean): void {
    if (this.connected === value) return;
    this.connected = value;
    for (const listener of this.statusListeners) listener();
  }

  /**
   * Update the set of session ids to watch. Sent to the server immediately
   * when connected. A no-op when the set is unchanged (order-insensitive),
   * so callers can recompute and push freely without churning the server.
   *
   * @param ids - Conversation ids currently displayed.
   */
  setWatched(ids: string[]): void {
    const key = [...ids].sort().join(",");
    if (key === this.watchedKey) return;
    this.watchedKey = key;
    this.watched = ids;
    this.sendWatch();
  }

  /**
   * Subscribe to parsed server frames.
   *
   * @param listener - Called for every frame received.
   * @returns An unsubscribe function.
   */
  subscribe(listener: FrameListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private connect(): void {
    let ws: WebSocket;
    try {
      ws = new WebSocket(buildUpdatesUrl());
    } catch (err) {
      // Construction can throw on a malformed URL / blocked context; treat
      // it as a failed open and retry with backoff.
      console.warn("[session-updates] WebSocket construction failed", err);
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.failedAttempts = 0;
      this.setConnected(true);
      // Start the silence watchdog: from here we expect at least a
      // heartbeat within the window or we treat the link as dead.
      this.armWatchdog();
      this.sendWatch();
    };
    ws.onmessage = (event) => this.handleMessage(event);
    ws.onerror = () => {
      // `onerror` is always followed by `onclose`; let close drive retry.
    };
    ws.onclose = () => {
      this.ws = null;
      this.clearWatchdog();
      this.setConnected(false);
      if (this.started) this.scheduleReconnect();
    };
  }

  /**
   * (Re)arm the silence watchdog. Called on connect and on every received
   * frame, so a steady stream of heartbeats / deltas keeps pushing the
   * deadline out; a stall trips {@link handleWatchdogExpiry}.
   */
  private armWatchdog(): void {
    this.clearWatchdog();
    this.watchdogTimer = setTimeout(() => {
      this.watchdogTimer = null;
      this.handleWatchdogExpiry();
    }, HEARTBEAT_WATCHDOG_MS);
  }

  private clearWatchdog(): void {
    if (this.watchdogTimer !== null) {
      clearTimeout(this.watchdogTimer);
      this.watchdogTimer = null;
    }
  }

  /**
   * No frame arrived within the watchdog window — assume the socket is
   * silently dead and force a reconnect. Closing it runs the normal
   * `onclose` path (mark disconnected → schedule reconnect with backoff),
   * which also flips consumers back to their HTTP fallback poll until the
   * stream is live again.
   */
  private handleWatchdogExpiry(): void {
    if (!this.started || this.ws === null) return;
    console.warn(`[session-updates] no frame in ${HEARTBEAT_WATCHDOG_MS} ms; reconnecting`);
    // close() drives onclose → setConnected(false) → scheduleReconnect.
    this.ws.close();
  }

  private scheduleReconnect(): void {
    if (!this.started || this.reconnectTimer !== null) return;
    this.failedAttempts += 1;
    const delay = nextReconnectDelay(this.failedAttempts);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.started) this.connect();
    }, delay);
  }

  private sendWatch(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "watch", session_ids: this.watched }));
    }
  }

  private handleMessage(event: MessageEvent): void {
    // Any inbound message — even a heartbeat or one we can't parse — proves
    // the link is alive, so push the silence deadline out.
    this.armWatchdog();
    if (typeof event.data !== "string") return;
    let frame: SessionUpdatesFrame;
    try {
      frame = JSON.parse(event.data) as SessionUpdatesFrame;
    } catch {
      return;
    }
    for (const listener of this.listeners) listener(frame);
  }
}

/** Shared transport instance for the current tab. */
export const sessionUpdatesSocket = new SessionUpdatesSocket();
