// Per-conversation event stream over a WebSocket (`WS /v1/sessions/{id}/stream/ws`).
//
// The functional twin of the SSE stream `openSessionStream` opens, but carried
// over a WebSocket so it rides the browser's separate (effectively unbounded)
// WS connection pool instead of the ~6-per-origin HTTP/1.1 pool. With several
// tabs open the SSE streams alone can exhaust that HTTP pool and stall every
// other request; this transport is the web client's default event stream.
//
// This module owns ONE connection's lifetime only — open, yield parsed events,
// end on close/abort. Reconnect (backoff, presence idle-flip recycle, snapshot
// reconcile) stays in `startStreamPump`, exactly as it does for the SSE path.
// The shape mirrors `parseSseStream(body, result)`: an async iterable of typed
// events plus a `result` out-param whose `sawCleanClose` distinguishes a
// deliberate server close from a transport drop.
//
// Identity rides the transport like the other app WebSockets: the browser
// can't set `X-Forwarded-Email` on a WS handshake, so we rely on the ingress /
// dev proxy to carry the authenticated identity. The server access-checks the
// session id on the connection's user — the id is never trusted for authz.

import type { StreamEvent } from "@/lib/events";
import { resolveWebSocketUrl } from "@/lib/host";
import { parseEvent } from "@/lib/sse";

/**
 * Out-param filled in as the stream ends, mirroring {@link SseStreamResult}.
 * `sawCleanClose` is `true` only when the server closed the socket normally
 * (code 1000) — the WS analog of the SSE `[DONE]` sentinel. A drop (abnormal
 * close, error, or a going-away code the server sends on subscriber overflow)
 * leaves it `false` so the reconnect loop re-subscribes.
 */
export interface WsStreamResult {
  sawCleanClose: boolean;
}

/** WebSocket normal-closure code — the clean-close (`[DONE]`-equivalent) signal. */
const WS_NORMAL_CLOSURE = 1000;

/**
 * Build the `ws(s)://` URL for a session's event stream, delegating to the
 * host seam like the session-updates socket.
 *
 * @param sessionId - Conversation id to stream.
 * @param idle - Connect-time presence idle flag (mirrors the SSE `?idle=`).
 * @returns The fully-qualified WebSocket URL.
 */
function buildEventStreamUrl(sessionId: string, idle: boolean): string {
  const query = idle ? "?idle=true" : "";
  return resolveWebSocketUrl(`/v1/sessions/${encodeURIComponent(sessionId)}/stream/ws${query}`);
}

/**
 * Open the per-conversation event WebSocket and yield parsed events until the
 * socket closes or `signal` aborts.
 *
 * Consumed by `pumpParsedEvents` in the store exactly like `parseSseStream`'s
 * output: the caller reduces the events into blocks and, when this iterable
 * ends, reads `result.sawCleanClose` to decide whether to reconnect.
 *
 * Aborting `signal` (switchTo / unmount / presence idle-flip) closes the
 * socket and ends iteration; the pump reads that as `"aborted"`. A network
 * drop or a server going-away close ends iteration with
 * `sawCleanClose === false`, which the loop treats as reconnectable.
 *
 * @param sessionId - Conversation id to stream.
 * @param signal - Abort signal owned by the caller's connection attempt.
 * @param opts - `idle` presence flag, forwarded to the server.
 * @param result - Out-param; `sawCleanClose` is set as the stream ends.
 * @returns An async iterable of typed {@link StreamEvent}s.
 */
export async function* streamSessionEventsWs(
  sessionId: string,
  signal: AbortSignal,
  opts: { idle?: boolean } | undefined,
  result: WsStreamResult,
): AsyncIterable<StreamEvent> {
  if (signal.aborted) return;

  const ws = new WebSocket(buildEventStreamUrl(sessionId, opts?.idle ?? false));

  // A single-slot handoff between the socket's event callbacks and this
  // generator: callbacks push events / a terminal sentinel and wake the
  // pending `next()`; the loop below awaits one at a time.
  const queue: StreamEvent[] = [];
  let ended = false;
  let wake: (() => void) | null = null;

  const signalReady = (): void => {
    if (wake) {
      const w = wake;
      wake = null;
      w();
    }
  };
  const nextReady = (): Promise<void> =>
    new Promise<void>((resolve) => {
      wake = resolve;
    });

  const finish = (): void => {
    if (ended) return;
    ended = true;
    signalReady();
  };

  ws.onmessage = (event) => {
    if (typeof event.data !== "string") return;
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(event.data) as Record<string, unknown>;
    } catch {
      return;
    }
    const type = payload["type"];
    if (typeof type !== "string") return;
    // The server sends the raw `ServerStreamEvent` payload as one JSON frame
    // (no SSE `event:`/`data:` envelope); the discriminant is the `type`
    // field, so parse against that — the same `parseEvent` the SSE path uses.
    const parsed = parseEvent(type, payload);
    if (parsed !== null) {
      queue.push(parsed);
      signalReady();
    }
  };
  ws.onclose = (event) => {
    // Normal closure is the server's deliberate end (`[DONE]` analog); any
    // other code is a drop the reconnect loop should recover from.
    result.sawCleanClose = event.code === WS_NORMAL_CLOSURE;
    finish();
  };
  ws.onerror = () => {
    // `onerror` precedes `onclose`; let close set the terminal state. Nothing
    // to do here beyond ensuring we don't hang if close never fires.
  };

  const onAbort = (): void => {
    // Caller tore down (switchTo / idle flip). Close the socket and end
    // iteration; `sawCleanClose` stays false so this reads as a drop, but the
    // pump maps an aborted attempt to `"aborted"` before that matters.
    try {
      ws.close();
    } catch {
      // Closing an already-closing socket can throw; ignore.
    }
    finish();
  };
  signal.addEventListener("abort", onAbort);

  try {
    while (true) {
      while (queue.length > 0) {
        yield queue.shift() as StreamEvent;
      }
      if (ended) return;
      // Serial by design: one frame arrives, is drained, then we await the
      // next; there is nothing to parallelize.
      // eslint-disable-next-line no-await-in-loop
      await nextReady();
    }
  } finally {
    signal.removeEventListener("abort", onAbort);
    // Emulate the async-iterator auto-cancel: a consumer that breaks out
    // (Stop pressed, session switched) must close the underlying socket
    // rather than leave it open until GC.
    if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
      try {
        ws.close();
      } catch {
        // Ignore close races.
      }
    }
  }
}
