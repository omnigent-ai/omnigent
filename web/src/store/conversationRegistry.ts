// Per-conversation state + stream ownership, and the LRU registry that holds
// them.
//
// Each entry owns ONE conversation: its `ConversationState` and its SSE stream
// (including the reconnect loop). An entry is either **live** — state plus an
// open stream — or **absent**. There is deliberately no third "retained but
// detached" state: a detached entry holding stale state that must be reconciled
// on revisit is a transcript cache in a different shape, which is what keeping
// streams open replaces. Live-or-gone is the simplification.

import type { ConversationState } from "./chatStore";
import { createInitialConversationState, isConversationStateKey } from "./conversationState";

/**
 * Whether the origin's transport multiplexes requests over one connection.
 *
 * `multiplexed` — HTTP/2 or HTTP/3: many concurrent streams on one connection.
 * `serial` — HTTP/1.1: the browser caps connections per origin, and an SSE
 * stream holds one for its entire life.
 */
export type ConnectionProtocol = "multiplexed" | "serial";

/** ALPN ids that multiplex. `h2c` is HTTP/2 over cleartext. */
const MULTIPLEXED_ALPN_IDS = new Set(["h2", "h2c", "h3"]);

/**
 * How the browser is actually talking to this origin.
 *
 * Prefers the navigation entry's `nextHopProtocol` — the negotiated ALPN id, so
 * the real answer — because the URL scheme does not imply a protocol: plenty of
 * HTTPS deployments still negotiate HTTP/1.1, and treating those as multiplexed
 * would deadlock the connection pool exactly like the dev server does.
 *
 * Falls back to the scheme only when the timing entry is unavailable (no
 * `performance` in the environment, an old browser, or a navigation the entry
 * doesn't cover), where `https:` is the better guess than `http:`.
 */
export function getConnectionProtocol(): ConnectionProtocol {
  const nextHop = readNextHopProtocol();
  if (nextHop !== null && nextHop !== "") {
    return MULTIPLEXED_ALPN_IDS.has(nextHop) ? "multiplexed" : "serial";
  }
  const scheme = typeof location === "undefined" ? undefined : location.protocol;
  return scheme === "https:" ? "multiplexed" : "serial";
}

/** The document navigation's negotiated ALPN id, or `null` when unavailable. */
function readNextHopProtocol(): string | null {
  if (typeof performance === "undefined" || typeof performance.getEntriesByType !== "function") {
    return null;
  }
  try {
    const [nav] = performance.getEntriesByType("navigation");
    const value = (nav as PerformanceNavigationTiming | undefined)?.nextHopProtocol;
    return typeof value === "string" ? value : null;
  } catch {
    // `getEntriesByType` is well-supported, but this runs on every eviction
    // check — a throw here must not take the registry down with it.
    return null;
  }
}

/**
 * How many conversations stay live at once.
 *
 * Derived from the transport, not from product taste. HTTP/2 multiplexes every
 * request to an origin over one TCP connection, so the ceiling is the server's
 * `SETTINGS_MAX_CONCURRENT_STREAMS` (~100). Plain HTTP/1.1 — the dev server,
 * which configures `server.proxy` with no `https` — is capped by the browser at
 * ~6 connections per origin, and an SSE stream holds one for its entire life.
 * Thirty streams there is not "slower", it is a deadlock: every ordinary fetch
 * blocks behind them forever.
 */
export function maxLiveConversations(
  protocol: ConnectionProtocol = getConnectionProtocol(),
): number {
  return protocol === "multiplexed" ? 30 : 3;
}

/** Writes a patch into a conversation's state. Mirrors zustand's `setState`. */
export type ConversationSetter = (
  partial: Partial<ConversationState> | ((state: ConversationState) => Partial<ConversationState>),
) => void;

/** Reads a conversation's current state. */
export type ConversationGetter = () => ConversationState;

/** Notified whenever an entry's state changes, so the root store can mirror it. */
type ChangeListener = (id: string) => void;

/**
 * One conversation's live state and stream.
 *
 * `disposed` is the **liveness** flag. The streaming machinery's loop and write
 * guards ask this — "am I still loaded?" — rather than "am I the conversation on
 * screen?", which is what let a background pump exit on its first reconnect
 * check. Nothing here knows or cares whether it is the active conversation.
 */
export interface ConversationEntry {
  readonly id: string;
  /** True once evicted or released. A disposed entry must stop pumping. */
  disposed: boolean;
  getState: ConversationGetter;
  setState: ConversationSetter;
  /** Tear down the stream and mark the entry dead. Idempotent. */
  dispose: () => void;
}

/**
 * Owns the set of live conversations.
 *
 * Insertion order is recency: `acquire` re-inserts, so the first key is the
 * least-recently-viewed and therefore the eviction candidate.
 */
export class ConversationRegistry {
  private readonly entries = new Map<string, ConversationEntry>();
  private readonly listeners = new Set<ChangeListener>();
  /** Conversation currently on screen; exempt from eviction. */
  private activeId: string | null = null;
  /** How many entries may be live. Injectable so tests can pin a small cap. */
  private readonly capacity: () => number;

  /**
   * :param capacity: live-entry budget, read on each eviction check. Defaults
   *     to the transport-derived {@link maxLiveConversations}.
   */
  constructor(capacity: () => number = maxLiveConversations) {
    this.capacity = capacity;
  }

  /**
   * Subscribe to state changes across all entries.
   *
   * The root store uses this to mirror the active entry. Returns an
   * unsubscribe function.
   */
  subscribe(listener: ChangeListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** The entry for `id`, or `undefined` when not live. */
  peek(id: string): ConversationEntry | undefined {
    return this.entries.get(id);
  }

  /** Whether `id` is currently live. */
  has(id: string): boolean {
    return this.entries.has(id);
  }

  /** Live entry ids, least-recently-viewed first. */
  ids(): string[] {
    return [...this.entries.keys()];
  }

  /** Every live entry. */
  all(): ConversationEntry[] {
    return [...this.entries.values()];
  }

  /**
   * Mark which conversation is on screen.
   *
   * Only used to exempt it from eviction — no behaviour is gated on being
   * active. Pass `null` on the landing route.
   *
   * Re-trims, because moving the exemption can make the OUTGOING conversation
   * evictable: an over-cap registry that only stayed over-cap to protect it
   * must shrink now, not wait for an unrelated acquire.
   */
  setActive(id: string | null): void {
    this.activeId = id;
    if (id !== null) this.touch(id);
    this.evictIfOverBudget(id ?? undefined);
  }

  /** The conversation on screen, or `null`. */
  getActive(): ConversationEntry | null {
    if (this.activeId === null) return null;
    return this.entries.get(this.activeId) ?? null;
  }

  /**
   * Get or create the entry for `id`, marking it most-recently-viewed.
   *
   * A fresh entry starts from `createInitialConversationState()`; the caller
   * binds its stream. Creating one may evict the least-recently-viewed entry.
   */
  acquire(id: string): ConversationEntry {
    const existing = this.entries.get(id);
    if (existing !== undefined) {
      this.touch(id);
      return existing;
    }
    const entry = this.createEntry(id);
    this.entries.set(id, entry);
    this.evictIfOverBudget(id);
    return entry;
  }

  /**
   * Drop `id` — on conversation delete, or when its stream is permanently
   * unavailable. Replaces the old `evictTranscriptCache`.
   */
  release(id: string): void {
    const entry = this.entries.get(id);
    if (entry === undefined) return;
    this.entries.delete(id);
    entry.dispose();
  }

  /** Drop every entry (app teardown / test reset). */
  clear(): void {
    for (const entry of [...this.entries.values()]) entry.dispose();
    this.entries.clear();
    this.activeId = null;
  }

  /** Move `id` to the most-recently-viewed end of the LRU order. */
  private touch(id: string): void {
    const entry = this.entries.get(id);
    if (entry === undefined) return;
    this.entries.delete(id);
    this.entries.set(id, entry);
  }

  /**
   * Evict least-recently-viewed entries until within budget.
   *
   * Protected from eviction: the conversation on screen, the entry just
   * acquired (evicting it would hand the caller a disposed entry), and any
   * entry holding work the server does not know about yet — see
   * `hasUnsentWork`. When every candidate is protected the map is allowed to
   * exceed the cap: going over budget costs memory and a connection slot, while
   * evicting would destroy a user's message.
   *
   * Runs from every path that can make an entry evictable — `acquire`, a
   * clearing pin, and `setActive` (which moves the on-screen exemption) —
   * because an over-cap registry has to shrink at that moment rather than
   * waiting for an unrelated acquire. Otherwise repeated send-and-switch leaves
   * the excess streams open indefinitely and can exhaust an HTTP/1.1 pool.
   *
   * :param justAcquiredId: entry the caller is about to be handed, if any.
   */
  private evictIfOverBudget(justAcquiredId?: string): void {
    const budget = this.capacity();
    for (const id of [...this.entries.keys()]) {
      if (this.entries.size <= budget) return;
      if (id === this.activeId || id === justAcquiredId) continue;
      const entry = this.entries.get(id);
      if (entry === undefined || hasUnsentWork(entry.getState())) continue;
      this.entries.delete(id);
      entry.dispose();
    }
  }

  private createEntry(id: string): ConversationEntry {
    let state = createInitialConversationState();
    const entry: ConversationEntry = {
      id,
      disposed: false,
      getState: () => state,
      setState: (partial) => {
        // A disposed entry still accepts reads, so in-flight async work can
        // unwind cleanly, but writes are dropped: nothing should be able to
        // resurrect state for a conversation that has been evicted.
        if (entry.disposed) return;
        const wasPinned = hasUnsentWork(state);
        const patch = typeof partial === "function" ? partial(state) : partial;
        let changed = false;
        const next = { ...state };
        for (const [key, value] of Object.entries(patch)) {
          if (!isConversationStateKey(key)) {
            // A key outside `ConversationState` means a caller is trying to
            // write app-global state through a conversation. Loud in dev
            // because the alternative is state that silently goes nowhere.
            if (import.meta.env?.DEV) {
              console.error(
                `conversation ${id}: ignoring non-conversation state key "${key}" — ` +
                  `app-global state belongs on the root store`,
              );
            }
            continue;
          }
          if (next[key as keyof ConversationState] !== value) changed = true;
          (next as Record<string, unknown>)[key] = value;
        }
        if (!changed) return;
        state = next;
        // A pin that just cleared may have been the only thing holding the map
        // over budget — see `evictIfOverBudget`. Trim before notifying, so a
        // listener never observes an over-cap registry that is about to shrink.
        if (wasPinned && !hasUnsentWork(state)) this.evictIfOverBudget();
        for (const listener of this.listeners) listener(id);
      },
      dispose: () => {
        if (entry.disposed) return;
        entry.disposed = true;
        // Ends the reconnect loop and cancels the in-flight fetch.
        state.abortController?.abort();
      },
    };
    return entry;
  }
}

/**
 * Whether an entry holds work the server has no record of.
 *
 * An unsettled optimistic bubble (`send`'s POST hasn't returned) exists nowhere
 * but this tab, so evicting the entry would lose the user's message outright —
 * the one case where dropping an entry is NOT equivalent to a cold load. This
 * is the hazard `pendingByConversation` was built to survive; pinning replaces
 * that stash.
 */
function hasUnsentWork(state: ConversationState): boolean {
  return state.pendingUserMessages.some((m) => m.posted !== true);
}

/** The app's registry. Module-scope, like the store it backs. */
export const conversationRegistry = new ConversationRegistry();
