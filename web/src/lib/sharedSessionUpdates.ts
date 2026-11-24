// A Web Lock elects one socket owner; followers use a BroadcastChannel and
// fall back to their own socket when the owner stops responding.

import type { SessionUpdatesFrame } from "./sessionUpdatesSocket";

/** The server truncates a watch-set past this many ids. */
export const SERVER_MAX_WATCHED = 500;
/** How long a starting tab waits for a leader before opening its own socket. */
export const JOIN_TIMEOUT_MS = 3_000;
// The server heartbeats every 30 s and the leader relays every frame, so a
// follower that hears nothing for a little over two heartbeats has a leader
// that stalled while still holding the lock (a frozen background tab).
export const LEADER_SILENCE_MS = 70_000;
/** How often the leader drops the ids of tabs that have gone away. */
export const MEMBER_PRUNE_INTERVAL_MS = 30_000;
/** Shortest gap between a detached tab's attempts to rejoin the leader. */
export const REJOIN_INTERVAL_MS = 5_000;
/** How long a tab the leader turned away waits before asking again with the same ids. */
export const REJECTION_RETRY_MS = 60_000;

const CHANNEL_PREFIX = "omnigent:session-updates:v1:";
const LEADER_LOCK_PREFIX = "omnigent:session-updates:v1:leader:";
const TAB_LOCK_PREFIX = "omnigent:session-updates:v1:tab:";

type FrameListener = (frame: SessionUpdatesFrame) => void;

/** The per-tab socket: what a tab uses alone, and what the leader shares. */
export interface UpdatesTransport {
  start: () => void;
  stop: () => void;
  isConnected: () => boolean;
  subscribeStatus: (listener: () => void) => () => void;
  /** `force` re-sends an unchanged set, so the server restates it in a snapshot. */
  setWatched: (ids: string[], options?: { force?: boolean }) => void;
  /**
   * Whether the server was watching `id` when it sent the frame now being
   * delivered. The set last asked for isn't it: the server may not have
   * processed that request yet.
   */
  isWatching: (id: string) => boolean;
  subscribe: (listener: FrameListener) => () => void;
}

/** The part of BroadcastChannel this module uses. */
export interface TabChannel {
  postMessage: (message: unknown) => void;
  onmessage: ((event: { data: unknown }) => void) | null;
  close: () => void;
}

/** What tabs need in common to share a socket. */
export interface SharedUpdatesEnvironment {
  locks: Pick<LockManager, "request" | "query">;
  openChannel: (name: string) => TabChannel;
  /** Names the server, its updates URL and the user; only tabs with the same scope share. */
  scope: string;
  /** Unique to this tab. */
  tabId: string;
}

type TabMessage =
  // Follower to leader.
  // `join` is set by a tab the leader isn't relaying to yet, which needs its rows restated.
  | { type: "watch"; from: string; ids: string[]; join?: boolean }
  | { type: "bye"; from: string }
  // Leader to followers.
  | { type: "leader"; leader: string }
  | { type: "accepted"; leader: string; to: string; connected: boolean }
  | { type: "rejected"; leader: string; to: string }
  | { type: "status"; leader: string; connected: boolean }
  // `covers` names the tabs whose every id a relayed snapshot's watch-set included.
  | {
      type: "frame";
      leader: string;
      frame: SessionUpdatesFrame;
      discovered: string[];
      covers?: string[];
    };

type Mode =
  | "stopped"
  // Sharing is unavailable: this tab's own socket, exactly as before.
  | "direct"
  // Asked to join and waiting for a leader's answer or for the lock.
  | "pending"
  // No socket; the leader relays.
  | "attached"
  // Its own socket, while still in line for the lock and willing to rejoin.
  | "detached"
  | "leader";

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

/** A message from another tab, or null for anything this build doesn't understand. */
function parseTabMessage(data: unknown): TabMessage | null {
  if (typeof data !== "object" || data === null) return null;
  const message = data as Record<string, unknown>;
  const fromFollower = typeof message.from === "string";
  const fromLeader = typeof message.leader === "string";
  switch (message.type) {
    case "watch":
      if (message.join !== undefined && typeof message.join !== "boolean") return null;
      return fromFollower && isStringArray(message.ids) ? (message as TabMessage) : null;
    case "bye":
      return fromFollower ? (message as TabMessage) : null;
    case "leader":
      return fromLeader ? (message as TabMessage) : null;
    case "accepted":
    case "status":
      if (!fromLeader || typeof message.connected !== "boolean") return null;
      if (message.type === "accepted" && typeof message.to !== "string") return null;
      return message as TabMessage;
    case "rejected":
      return fromLeader && typeof message.to === "string" ? (message as TabMessage) : null;
    case "frame":
      return fromLeader &&
        typeof message.frame === "object" &&
        message.frame !== null &&
        isStringArray(message.discovered) &&
        (message.covers === undefined || isStringArray(message.covers))
        ? (message as TabMessage)
        : null;
    default:
      return null;
  }
}

/**
 * The part of a frame from the shared socket that concerns one tab.
 *
 * The socket watches every tab's ids, so a snapshot or delta can carry rows
 * only another tab is showing. Rows the server announced on its own (a session
 * just created or shared, in `discovered`) go to every tab, as they would on a
 * tab's own socket.
 *
 * @returns The frame to deliver, or null when nothing in it is for this tab.
 */
export function frameForTab(
  frame: SessionUpdatesFrame,
  watched: ReadonlySet<string>,
  discovered: ReadonlySet<string>,
): SessionUpdatesFrame | null {
  switch (frame.type) {
    case "snapshot":
    case "changed": {
      const items = frame.items.filter((item) => watched.has(item.id) || discovered.has(item.id));
      if (items.length === frame.items.length) return frame;
      // A snapshot restates a watch-set. One with none of this tab's ids was
      // taken before the leader knew them, and would read as "all removed".
      if (items.length === 0 && (frame.type === "changed" || watched.size > 0)) return null;
      return { ...frame, items };
    }
    case "removed": {
      const ids = frame.ids.filter((id) => watched.has(id));
      if (ids.length === frame.ids.length) return frame;
      return ids.length === 0 ? null : { ...frame, ids };
    }
    default:
      return frame;
  }
}

function watchKey(ids: string[]): string {
  return [...ids].sort().join(",");
}

/**
 * A tab's session-updates connection: its own socket, or a share of the
 * leader tab's.
 *
 * Has the transport's interface, so callers don't know which they have.
 *
 * @param transport - This tab's socket. Runs only while the tab is alone,
 *   detached or the leader.
 * @param resolveEnvironment - Called on each `start()`; null keeps the tab on
 *   its own socket.
 * @param eligibilityPending - Called when `resolveEnvironment` returned null.
 *   A promise means the answer isn't known yet (the server's features are
 *   still loading); once it settles the tab asks again and joins if it may.
 */
export class SharedSessionUpdates implements Omit<UpdatesTransport, "isWatching"> {
  private readonly transport: UpdatesTransport;
  private readonly resolveEnvironment: () => SharedUpdatesEnvironment | null;
  private readonly eligibilityPending: () => Promise<unknown> | null;
  private readonly listeners = new Set<FrameListener>();
  private readonly statusListeners = new Set<() => void>();
  private mode: Mode = "stopped";
  private connected = false;
  private watched: string[] = [];
  private watchedSet: ReadonlySet<string> = new Set();
  private watchedKey = "";

  private environment: SharedUpdatesEnvironment | null = null;
  private channel: TabChannel | null = null;
  private stopController: AbortController | null = null;
  private releaseLocks: () => void = () => {};

  // Follower state.
  private leaderId: string | null = null;
  private leaderConnected = false;
  private joinTimer: ReturnType<typeof setTimeout> | null = null;
  private silenceTimer: ReturnType<typeof setTimeout> | null = null;
  private lastJoinAttemptAt: number | null = null;
  // The leader that turned this tab away, the set it turned away, and when.
  private rejection: { leader: string; watchedKey: string; at: number } | null = null;
  // Attached, but still on this tab's own socket until the leader relays a snapshot.
  private handingOff = false;

  // Leader state: every other tab's ids, in the order the tabs joined.
  private readonly members = new Map<string, string[]>();
  private pruneTimer: ReturnType<typeof setInterval> | null = null;
  // Whether this tab holds its presence lock, which is what tells a leader it is alive.
  private presenceHeld = false;
  // Counts `start()`s that went direct, so a late eligibility answer can't act on a later one.
  private directSession = 0;

  constructor(
    transport: UpdatesTransport,
    resolveEnvironment: () => SharedUpdatesEnvironment | null,
    eligibilityPending: () => Promise<unknown> | null = () => null,
  ) {
    this.transport = transport;
    this.resolveEnvironment = resolveEnvironment;
    this.eligibilityPending = eligibilityPending;
    transport.subscribe((frame) => this.onTransportFrame(frame));
    transport.subscribeStatus(() => this.onTransportStatus());
  }

  /** Connect: join the tabs sharing a socket, or open this tab's own (idempotent). */
  start(): void {
    if (this.mode !== "stopped") return;
    const environment = this.resolveEnvironment();
    if (environment === null) {
      this.mode = "direct";
      this.transport.start();
      this.syncConnected();
      const pending = this.eligibilityPending();
      const session = ++this.directSession;
      void pending?.then(
        () => this.joinFromDirect(session),
        () => {},
      );
      return;
    }
    this.joinGroup(environment, () => this.enterPending());
  }

  /** Sharing became possible after this tab opened its own socket: join without dropping it. */
  private joinFromDirect(session: number): void {
    if (this.mode !== "direct" || session !== this.directSession) return;
    const environment = this.resolveEnvironment();
    if (environment === null) return;
    // Like a tab that fell back: it keeps its socket until a leader has its rows.
    this.joinGroup(environment, () => {
      this.mode = "detached";
    });
  }

  /**
   * Open the channel and get in line for the leader lock.
   *
   * @param enter - Puts the tab in its starting state. Runs before the lock
   *   requests: a free lock makes this tab the leader at once, which must not
   *   then be reset to a follower.
   */
  private joinGroup(environment: SharedUpdatesEnvironment, enter: () => void): void {
    this.environment = environment;
    const channel = environment.openChannel(`${CHANNEL_PREFIX}${environment.scope}`);
    channel.onmessage = (event) => this.onTabMessage(event.data);
    this.channel = channel;
    const stopController = new AbortController();
    this.stopController = stopController;
    const untilStopped = new Promise<void>((resolve) => {
      this.releaseLocks = resolve;
    });
    const options = { signal: stopController.signal };
    enter();
    // Held for the tab's life, so the leader can tell a closed tab from a quiet
    // one. The tab asks to join only once it holds it: a leader that looked for
    // the lock any earlier would take the tab for a closed one.
    environment.locks
      .request(this.tabLockName(environment.tabId), options, () => {
        this.presenceHeld = true;
        if (this.mode === "pending" || this.mode === "detached") this.postWatch();
        return untilStopped;
      })
      .catch(() => {});
    environment.locks
      .request(`${LEADER_LOCK_PREFIX}${environment.scope}`, options, () => {
        this.becomeLeader();
        return untilStopped;
      })
      .catch(() => {});
  }

  /** Disconnect and leave the group; another tab takes over a leader's socket. */
  stop(): void {
    if (this.mode === "stopped") return;
    const wasFollower = this.mode === "pending" || this.mode === "attached";
    this.mode = "stopped";
    this.clearFollowerTimers();
    if (this.pruneTimer !== null) clearInterval(this.pruneTimer);
    this.pruneTimer = null;
    if (wasFollower && this.environment !== null) {
      this.post({ type: "bye", from: this.environment.tabId });
    }
    if (this.channel !== null) {
      this.channel.onmessage = null;
      this.channel.close();
    }
    this.channel = null;
    this.stopController?.abort();
    this.stopController = null;
    this.releaseLocks();
    this.releaseLocks = () => {};
    this.environment = null;
    this.members.clear();
    this.presenceHeld = false;
    this.leaderId = null;
    this.leaderConnected = false;
    this.lastJoinAttemptAt = null;
    this.rejection = null;
    this.handingOff = false;
    this.directSession += 1;
    this.transport.stop();
    // A later start() may run alone, so the socket must hold this tab's own ids.
    this.transport.setWatched(this.watched);
    this.syncConnected();
  }

  /** Whether updates are arriving: this tab's socket is open, or the leader's is. */
  isConnected(): boolean {
    return this.connected;
  }

  subscribeStatus(listener: () => void): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  /**
   * Update the session ids this tab is displaying. A no-op when the set is
   * unchanged (order-insensitive).
   */
  setWatched(ids: string[]): void {
    const key = watchKey(ids);
    if (key === this.watchedKey) return;
    const previous = this.watchedSet;
    this.watched = ids;
    this.watchedSet = new Set(ids);
    this.watchedKey = key;
    switch (this.mode) {
      case "pending":
      case "attached":
        this.postWatch();
        return;
      case "leader":
        this.watchUnion(ids.some((id) => !previous.has(id)));
        return;
      case "detached":
        this.transport.setWatched(ids);
        // A set the leader turned away may fit now.
        if (this.leaderId !== null) this.tryRejoin(this.leaderId);
        return;
      default:
        this.transport.setWatched(ids);
    }
  }

  subscribe(listener: FrameListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private tabLockName(tabId: string): string {
    return `${TAB_LOCK_PREFIX}${this.environment?.scope ?? ""}:${tabId}`;
  }

  private post(message: TabMessage): void {
    this.channel?.postMessage(message);
  }

  private postWatch(): void {
    if (this.environment === null || !this.presenceHeld) return;
    this.post({
      type: "watch",
      from: this.environment.tabId,
      ids: this.watched,
      join: this.mode !== "attached",
    });
  }

  private deliver(frame: SessionUpdatesFrame): void {
    for (const listener of this.listeners) listener(frame);
  }

  private syncConnected(): void {
    let connected = false;
    if (this.mode === "attached") {
      connected = this.leaderConnected || (this.handingOff && this.transport.isConnected());
    } else if (this.mode !== "stopped" && this.mode !== "pending") {
      connected = this.transport.isConnected();
    }
    if (connected === this.connected) return;
    this.connected = connected;
    for (const listener of this.statusListeners) listener();
  }

  private clearFollowerTimers(): void {
    if (this.joinTimer !== null) clearTimeout(this.joinTimer);
    this.joinTimer = null;
    if (this.silenceTimer !== null) clearTimeout(this.silenceTimer);
    this.silenceTimer = null;
  }

  // ── This tab's socket ──────────────────────────────────────────────────

  private onTransportFrame(frame: SessionUpdatesFrame): void {
    if (this.mode === "direct" || this.mode === "detached" || this.handingOff) {
      this.deliver(frame);
      return;
    }
    if (this.mode !== "leader" || this.environment === null) return;
    // The server announces a session on its own only in a delta, and only one
    // it wasn't watching when it sent the delta. What this tab last asked for
    // doesn't say: the server may be behind it, in either direction.
    const discovered =
      frame.type === "changed"
        ? frame.items.map((item) => item.id).filter((id) => !this.transport.isWatching(id))
        : [];
    // A snapshot restates what the server was watching; tell tabs waiting on
    // one whether it had all of their ids.
    const covers =
      frame.type === "snapshot"
        ? [...this.members]
            .filter(([, ids]) => ids.every((id) => this.transport.isWatching(id)))
            .map(([member]) => member)
        : undefined;
    this.post({ type: "frame", leader: this.environment.tabId, frame, discovered, covers });
    const own = frameForTab(frame, this.watchedSet, new Set(discovered));
    if (own !== null) this.deliver(own);
  }

  private onTransportStatus(): void {
    if (this.mode === "leader" && this.environment !== null) {
      this.post({
        type: "status",
        leader: this.environment.tabId,
        connected: this.transport.isConnected(),
      });
    }
    this.syncConnected();
  }

  // ── Follower ───────────────────────────────────────────────────────────

  private enterPending(): void {
    this.clearFollowerTimers();
    this.endHandoff();
    this.mode = "pending";
    this.leaderConnected = false;
    this.syncConnected();
    this.postWatch();
    this.joinTimer = setTimeout(() => {
      this.joinTimer = null;
      if (this.mode === "pending") this.detach();
    }, JOIN_TIMEOUT_MS);
  }

  /** Use this tab's own socket, staying in line for the lock. */
  private detach(): void {
    this.clearFollowerTimers();
    this.handingOff = false;
    this.mode = "detached";
    this.transport.setWatched(this.watched);
    this.transport.start();
    this.syncConnected();
  }

  private attach(leader: string, connected: boolean): void {
    // A tab on its own socket keeps it until the leader has restated its rows,
    // so no update falls between the two.
    if (this.mode === "detached") this.handingOff = true;
    if (this.joinTimer !== null) clearTimeout(this.joinTimer);
    this.joinTimer = null;
    this.mode = "attached";
    this.leaderId = leader;
    this.leaderConnected = connected;
    this.rejection = null;
    this.armSilenceTimer();
    this.syncConnected();
  }

  /** Close this tab's own socket, now that the leader's covers its rows. */
  private endHandoff(): void {
    if (!this.handingOff) return;
    this.handingOff = false;
    this.transport.stop();
  }

  private armSilenceTimer(): void {
    if (this.silenceTimer !== null) clearTimeout(this.silenceTimer);
    this.silenceTimer = setTimeout(() => {
      this.silenceTimer = null;
      if (this.mode === "attached") this.detach();
    }, LEADER_SILENCE_MS);
  }

  /** Ask a leader that is evidently alive to take this detached tab back. */
  private tryRejoin(leader: string): void {
    this.leaderId = leader;
    const now = Date.now();
    // Other tabs may have freed room since, so a refusal is only believed for a while.
    if (
      this.rejection?.leader === leader &&
      this.rejection.watchedKey === this.watchedKey &&
      now - this.rejection.at < REJECTION_RETRY_MS
    ) {
      return;
    }
    if (this.lastJoinAttemptAt !== null && now - this.lastJoinAttemptAt < REJOIN_INTERVAL_MS) {
      return;
    }
    this.lastJoinAttemptAt = now;
    this.postWatch();
  }

  private onLeaderMessage(message: Exclude<TabMessage, { from: string }>): void {
    const tabId = this.environment?.tabId;
    if (message.type === "accepted") {
      if (message.to === tabId) this.attach(message.leader, message.connected);
      return;
    }
    if (message.type === "rejected") {
      if (message.to !== tabId) return;
      this.leaderId = message.leader;
      this.rejection = { leader: message.leader, watchedKey: this.watchedKey, at: Date.now() };
      if (this.mode !== "detached") this.detach();
      return;
    }
    if (this.mode === "detached") {
      this.tryRejoin(message.leader);
      return;
    }
    // A leader this tab hasn't joined: a new one took over, so join it.
    if (message.type === "leader" || message.leader !== this.leaderId) {
      this.leaderId = null;
      this.enterPending();
      return;
    }
    if (this.mode !== "attached") return;
    this.armSilenceTimer();
    if (message.type === "status") {
      this.leaderConnected = message.connected;
      this.syncConnected();
      return;
    }
    const own = frameForTab(message.frame, this.watchedSet, new Set(message.discovered));
    if (own !== null) this.deliver(own);
    // Only a snapshot taken with all of this tab's ids replaces its own socket.
    if (tabId !== undefined && message.covers?.includes(tabId)) this.endHandoff();
  }

  // ── Leader ─────────────────────────────────────────────────────────────

  private becomeLeader(): void {
    if (this.mode === "stopped" || this.environment === null) return;
    this.clearFollowerTimers();
    // This tab's own socket, if it has one open, becomes the shared one.
    this.handingOff = false;
    this.mode = "leader";
    this.leaderId = this.environment.tabId;
    this.rejection = null;
    this.members.clear();
    this.watchUnion(false);
    this.transport.start();
    // Followers of the previous leader answer this by sending their ids.
    this.post({ type: "leader", leader: this.environment.tabId });
    this.pruneTimer = setInterval(() => void this.pruneMembers(), MEMBER_PRUNE_INTERVAL_MS);
    this.syncConnected();
  }

  /**
   * Point the socket at every tab's ids, this tab's first. Tabs whose ids
   * don't fit under the server's cap are turned away, the latest to join first.
   */
  private watchUnion(force: boolean): void {
    if (this.environment === null) return;
    const union = new Set(this.watched);
    for (const [member, ids] of [...this.members]) {
      const size = union.size;
      for (const id of ids) union.add(id);
      if (union.size <= SERVER_MAX_WATCHED) continue;
      for (const id of ids) if (!this.watchedSet.has(id)) union.delete(id);
      // Ids an earlier member also watches were dropped with the rest; put them back.
      for (const [kept, keptIds] of this.members) {
        if (kept === member) break;
        for (const id of keptIds) union.add(id);
      }
      if (union.size !== size) continue;
      this.members.delete(member);
      this.post({ type: "rejected", leader: this.environment.tabId, to: member });
    }
    this.transport.setWatched([...union], { force });
  }

  private onFollowerMessage(message: Extract<TabMessage, { from: string }>): void {
    if (this.environment === null) return;
    if (message.type === "bye") {
      if (this.members.delete(message.from)) this.watchUnion(false);
      return;
    }
    const previous = new Set(this.members.get(message.from) ?? []);
    // Re-inserted last, so a set that no longer fits turns away its own tab.
    this.members.delete(message.from);
    this.members.set(message.from, message.ids);
    // A snapshot restates the ids this tab just started showing, as its own
    // socket's would, and every id of a tab that is only now joining.
    this.watchUnion(message.join === true || message.ids.some((id) => !previous.has(id)));
    if (!this.members.has(message.from)) return;
    this.post({
      type: "accepted",
      leader: this.environment.tabId,
      to: message.from,
      connected: this.transport.isConnected(),
    });
  }

  /** Stop watching for tabs that closed without saying so. */
  private async pruneMembers(): Promise<void> {
    const environment = this.environment;
    if (environment === null || this.members.size === 0) return;
    // The lock list is read as of now. A tab that registers, or registers
    // again, while the answer is on its way is alive and must not be judged by it.
    const candidates = new Map(this.members);
    let held: LockInfo[];
    try {
      held = (await environment.locks.query()).held ?? [];
    } catch {
      return;
    }
    if (this.mode !== "leader" || this.environment !== environment) return;
    const live = new Set(held.map((lock) => lock.name));
    let removed = false;
    for (const [member, ids] of candidates) {
      if (live.has(this.tabLockName(member)) || this.members.get(member) !== ids) continue;
      this.members.delete(member);
      removed = true;
    }
    if (removed) this.watchUnion(false);
  }

  private onTabMessage(data: unknown): void {
    const message = parseTabMessage(data);
    if (message === null || this.mode === "stopped" || this.mode === "direct") return;
    if ("from" in message) {
      if (this.mode === "leader") this.onFollowerMessage(message);
      return;
    }
    // The lock admits one leader, so another leader's messages can only be stale.
    if (this.mode !== "leader") this.onLeaderMessage(message);
  }
}
