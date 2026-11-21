// The one owner of each session's branch-point comparison. Every view of a
// session's changes registers here, including grid panes, so they share one
// route watch, one listing in flight, one established comparison, and one
// "Show uncommitted changes" choice. Listing runs only while a registered view
// may read the workspace; polling runs only while a Changes list or review is
// visible. Views read the state through useSyncExternalStore.

import { type ComparisonWorkspace, type RevisionSpec, sameRevisions } from "@/lib/customComparison";
import {
  type BranchPointComparison,
  type BranchPointListing,
  type BranchPointSupport,
  type ComparisonProblem,
  branchPointSupport,
  listingProblemFrom,
  revisionSupport,
  type WorkspaceGitRoute,
} from "@/lib/workingCopyComparison";
import type { WorkingCopyBaseIntent } from "@/lib/workingCopyBasePreference";

/** How a view uses the session's changes; only visible surfaces poll. */
export type ComparisonConsumerKind = "count" | "surface" | "file";

export interface ComparisonConsumer {
  kind: ComparisonConsumerKind;
  /** Whether this view may read the workspace now: permission, serveability, environment. */
  enabled: boolean;
  sessionHostId: string | null;
  /** The session's workspace root, once known; a typed comparison belongs to it. */
  root?: string | null;
}

/** What views read about one session's comparison. */
export interface ComparisonOwnerSnapshot {
  /** Bumped when the owner drops everything; a read from an older one is stale. */
  generation: number;
  support: BranchPointSupport;
  /** Whether a typed base and head can be compared for this session. */
  revisionSupport: BranchPointSupport;
  /** The base and head the person typed for this session, which win over the saved base. */
  custom: RevisionSpec | null;
  /** The workspace typed comparisons and their suggestions belong to, once known. */
  workspace: ComparisonWorkspace | null;
  established: BranchPointComparison | null;
  problem: ComparisonProblem | null;
  listing: boolean;
  connectTimedOut: boolean;
  headOverride: boolean;
  /** Bumped when the session's files may be read again; readers retry then. */
  accessEpoch: number;
}

export interface ComparisonOwnerDependencies {
  fetchListing: (
    sessionId: string,
    options: { refresh: boolean; signal: AbortSignal; spec: RevisionSpec | null },
  ) => Promise<BranchPointListing>;
  /** Asks whether the session's host serves comparisons, again on `ask`. */
  watchRoute: (
    target: { sessionId: string },
    onRoute: (route: WorkspaceGitRoute) => void,
  ) => { stop: () => void; ask: () => void };
  readIntent: () => WorkingCopyBaseIntent;
  subscribeIntent: (listener: () => void) => () => void;
  now: () => number;
  setTimeout: (callback: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
  documentVisible: () => boolean;
  subscribeAttention: (listener: () => void) => () => void;
  /** Forgets the files a session listed, once they belong to a workspace it left. */
  forgetListings?: (sessionId: string) => void;
  /** Hears that a session's files may be read again. */
  subscribeAccessReady?: (listener: (sessionId: string) => void) => () => void;
  /** Each session's typed comparison per workspace, saved so that every window and reload shows it. */
  customComparisons?: {
    read: (sessionId: string, workspace: ComparisonWorkspace) => RevisionSpec | null;
    write: (sessionId: string, workspace: ComparisonWorkspace, spec: RevisionSpec) => void;
    clear: (sessionId: string, workspace: ComparisonWorkspace) => void;
    subscribe: (listener: () => void) => () => void;
  };
}

/** Re-list visible changes this often, and more slowly after a slow or failed list. */
export const POLL_MS = 30_000;
export const SLOW_POLL_MS = 120_000;
/** A list slower than this switches to the slow poll. */
export const SLOW_LIST_MS = 2_000;
/** Automatic shows uncommitted changes if no comparison arrives within this. */
export const CONNECT_FALLBACK_MS = 30_000;
/** Focus and visibility don't re-list a comparison listed more recently than this. */
export const ATTENTION_MIN_INTERVAL_MS = 10_000;
/** How long an unwatched session keeps its route watch, so a quick remount reuses it. */
export const IDLE_GRACE_MS = 30_000;
/** An unwatched session forgets its comparison and choices after this. */
export const IDLE_TTL_MS = 10 * 60_000;

/** Problems after which polling slows, since the next list will likely be as slow. */
const SLOW_PROBLEMS = new Set(["timeout", "too_many_files", "listing_too_large"]);

/** One file's automatic re-list: in progress, or spent once its list finished. */
interface FileRecovery {
  pending: Promise<boolean> | null;
  /** `listsFinished` once the recovery's list ended; reads begun before it get one more try. */
  finishedAt: number | null;
}

interface SessionState {
  sessionId: string;
  consumers: Map<symbol, ComparisonConsumer>;
  route: WorkspaceGitRoute | null;
  watch: { stop: () => void; ask: () => void } | null;
  /** The workspace the session's views last reported; typed comparisons are kept per workspace. */
  workspace: ComparisonWorkspace | null;
  custom: RevisionSpec | null;
  established: BranchPointComparison | null;
  problem: ComparisonProblem | null;
  inFlight: AbortController | null;
  dirty: boolean;
  refreshQueued: boolean;
  attempted: boolean;
  lastFinishedAt: number;
  slow: boolean;
  connectTimer: unknown;
  connectTimedOut: boolean;
  pollTimer: unknown;
  idleTimer: unknown;
  idleTtlTimer: unknown;
  headOverride: boolean;
  accessEpoch: number;
  /** When the route was last asked again about a comparison policy that was off. */
  lastPolicyAskAt: number;
  /** Paths whose automatic re-list was spent; each file has its own chain. */
  recoveryUsed: Map<string, FileRecovery>;
  /** Counts lists started, so a recovery waits for one that began after it asked. */
  listSerial: number;
  /** Counts lists finished, so a reader can say which listing its read began under. */
  listsFinished: number;
  recoveryWaiters: { after: number; resolve: (relisted: boolean) => void }[];
  listeners: Set<() => void>;
  snapshot: ComparisonOwnerSnapshot;
}

export class WorkingCopyComparisonOwner {
  private readonly sessions = new Map<string, SessionState>();
  private readonly deps: ComparisonOwnerDependencies;
  private generation = 0;
  private intent: WorkingCopyBaseIntent;
  private readonly unsubscribes: (() => void)[] = [];

  constructor(deps: ComparisonOwnerDependencies) {
    this.deps = deps;
    this.intent = deps.readIntent();
    this.unsubscribes.push(
      deps.subscribeIntent(() => {
        const next = this.deps.readIntent();
        if (next === this.intent) return;
        this.intent = next;
        // A new choice, from any window, replaces each session's own override.
        for (const session of this.sessions.values()) this.clearChoiceState(session);
        for (const session of this.sessions.values()) this.pump(session);
      }),
      deps.subscribeAttention(() => {
        for (const session of this.sessions.values()) this.attention(session);
      }),
    );
    const unsubscribeAccess = deps.subscribeAccessReady?.((sessionId) => {
      const session = this.sessions.get(sessionId);
      if (!session) return;
      // Verified again: list once more, and let readers retry what was refused meanwhile.
      session.accessEpoch += 1;
      session.dirty = true;
      this.publish(session);
      this.pump(session);
    });
    if (unsubscribeAccess) this.unsubscribes.push(unsubscribeAccess);
    const unsubscribeCustom = deps.customComparisons?.subscribe(() => {
      for (const session of this.sessions.values()) {
        this.applyCustom(session, this.readCustom(session));
      }
    });
    if (unsubscribeCustom) this.unsubscribes.push(unsubscribeCustom);
  }

  // ── Views ─────────────────────────────────────────────────────────────────

  /** Adds a view of `sessionId`; returns its handle for update and unregister. */
  register(sessionId: string, consumer: ComparisonConsumer): symbol {
    const session = this.session(sessionId);
    const token = Symbol(consumer.kind);
    const wasActive = this.active(session);
    session.consumers.set(token, consumer);
    this.clearIdleTimers(session);
    this.syncWorkspace(session);
    this.publish(session);
    // A view that just appeared shows the latest state, within the attention limit.
    if (!wasActive || consumer.kind === "surface") this.attention(session);
    this.pump(session);
    return token;
  }

  update(sessionId: string, token: symbol, consumer: ComparisonConsumer): void {
    const session = this.sessions.get(sessionId);
    if (!session?.consumers.has(token)) return;
    const previous = session.consumers.get(token)!;
    session.consumers.set(token, consumer);
    // Reachable again, as after a reconnect: list now, whatever the attention limit says.
    if (!previous.enabled && consumer.enabled) session.dirty = true;
    this.syncWorkspace(session);
    this.publish(session);
    this.pump(session);
  }

  unregister(sessionId: string, token: symbol): void {
    const session = this.sessions.get(sessionId);
    if (!session?.consumers.delete(token)) return;
    this.publish(session);
    this.pump(session);
  }

  subscribe(sessionId: string, listener: () => void): () => void {
    const session = this.session(sessionId);
    session.listeners.add(listener);
    return () => session.listeners.delete(listener);
  }

  snapshot(sessionId: string): ComparisonOwnerSnapshot {
    return this.session(sessionId).snapshot;
  }

  // ── Actions ───────────────────────────────────────────────────────────────

  /** The session's files may have changed: list again once the current list ends. */
  invalidate(sessionId: string): void {
    const session = this.sessions.get(sessionId);
    if (!session) return;
    session.dirty = true;
    this.pump(session);
  }

  /** A new comparison, and a new ledger, even if nothing moved. */
  refresh(sessionId: string): void {
    const session = this.session(sessionId);
    this.clearChoiceState(session);
    session.refreshQueued = true;
    session.dirty = true;
    session.watch?.ask();
    this.pump(session);
  }

  /** Try listing again after a failure; files' own recovery chains stay as they are. */
  retry(sessionId: string): void {
    const session = this.session(sessionId);
    session.connectTimedOut = false;
    session.dirty = true;
    session.watch?.ask();
    this.pump(session);
  }

  /** Show uncommitted changes for this session until a base is chosen again; a typed comparison ends. */
  showLastCommit(sessionId: string): void {
    const session = this.session(sessionId);
    this.clearCustom(session);
    session.headOverride = true;
    this.abortList(session);
    this.publish(session);
    this.pump(session);
  }

  /** The person chose a base, even the current one: their override and typed comparison end. */
  chose(sessionId: string): void {
    const session = this.session(sessionId);
    this.clearCustom(session);
    this.clearChoiceState(session);
    session.dirty = session.dirty || session.established === null;
    this.pump(session);
  }

  /**
   * Compares the session's typed base and head, saved for its workspace, in
   * place of its saved base. The same comparison again lists it again. Ignored
   * until the session's workspace is known.
   */
  compareRevisions(sessionId: string, spec: RevisionSpec): void {
    const session = this.session(sessionId);
    if (!session.workspace) return;
    this.deps.customComparisons?.write(sessionId, session.workspace, spec);
    this.applyCustom(session, spec);
    this.clearChoiceState(session);
    session.dirty = true;
    session.watch?.ask();
    this.pump(session);
  }

  /** Whether some view may read the session's workspace now; reads start only then. */
  mayRead(sessionId: string): boolean {
    const session = this.sessions.get(sessionId);
    return session !== undefined && this.active(session);
  }

  /** Names the listing a read begins under, for {@link recover}. */
  readToken(sessionId: string): number {
    return this.sessions.get(sessionId)?.listsFinished ?? 0;
  }

  /**
   * A read of `path` found the comparison gone or the file not in it. The
   * session lists again once per file, and every reader of that file shares
   * the chain: a reader joins one in progress, and a read that began before
   * the chain's list finished tries once more under it. A refusal after that
   * is the reader's error until {@link allowRecovery}. The chain spans a new id.
   *
   * :param readToken: {@link readToken} when the refused read began.
   * :returns: Resolves once a list that began after the first refusal ends:
   *   true when the reader should try again under the snapshot, false when
   *   its chain was spent.
   */
  recover(
    sessionId: string,
    comparisonId: string,
    path: string,
    readToken = Number.POSITIVE_INFINITY,
  ): Promise<boolean> {
    const session = this.sessions.get(sessionId);
    if (!session) return Promise.resolve(false);
    // A newer comparison already replaced it; the reader re-keys to that.
    if (session.established && session.established.id !== comparisonId) {
      return Promise.resolve(true);
    }
    const existing = session.recoveryUsed.get(path);
    if (existing?.pending) return existing.pending;
    if (existing) {
      // Another reader's refusal started this chain; this read predates its list.
      return Promise.resolve(existing.finishedAt !== null && readToken < existing.finishedAt);
    }
    const recovery: FileRecovery = { pending: null, finishedAt: null };
    session.recoveryUsed.set(path, recovery);
    session.dirty = true;
    recovery.pending = new Promise<boolean>((resolve) => {
      session.recoveryWaiters.push({ after: session.listSerial, resolve });
    }).then((relisted) => {
      recovery.pending = null;
      recovery.finishedAt = session.listsFinished;
      return relisted;
    });
    const pending = recovery.pending;
    this.pump(session);
    return pending;
  }

  /** A read of `path` under the current comparison worked, so that file's chain ended. */
  readSucceeded(sessionId: string, comparisonId: string, path: string): void {
    const session = this.sessions.get(sessionId);
    if (session?.established?.id !== comparisonId) return;
    // A chain still in progress belongs to other readers; it ends when its list does.
    if (!session.recoveryUsed.get(path)?.pending) session.recoveryUsed.delete(path);
  }

  /** Allows `path` another automatic recovery, after the person asked to try it again. */
  allowRecovery(sessionId: string, path: string): void {
    const session = this.sessions.get(sessionId);
    if (session && !session.recoveryUsed.get(path)?.pending) session.recoveryUsed.delete(path);
  }

  /**
   * Whether `comparisonId` and the file's fingerprint are still what this
   * session shows, to a view that may read it.
   */
  isCurrent(
    sessionId: string,
    generation: number,
    comparisonId: string,
    path?: string,
    fingerprint?: string,
  ): boolean {
    const session = this.sessions.get(sessionId);
    if (!session || generation !== this.generation) return false;
    // Nobody may read the workspace now, as after a lost grant: no answer is shown.
    if (!this.active(session)) return false;
    const established = session.established;
    if (!established || established.id !== comparisonId) return false;
    if (path === undefined) return true;
    return established.entries.some(
      (entry) => entry.path === path && entry.fingerprint === fingerprint,
    );
  }

  /** Drops every session's state, as when the signed-in user changes. Views stay registered. */
  reset(): void {
    this.generation += 1;
    for (const session of this.sessions.values()) {
      this.abortList(session);
      this.stopTimers(session);
      session.watch?.stop();
      session.watch = null;
      session.route = null;
      this.forget(session);
      session.custom = this.readCustom(session);
      this.publish(session);
      this.pump(session);
    }
  }

  dispose(): void {
    this.reset();
    for (const unsubscribe of this.unsubscribes) unsubscribe();
    for (const session of this.sessions.values()) session.watch?.stop();
    this.sessions.clear();
  }

  // ── Scheduling ───────────────────────────────────────────────────────────

  private session(sessionId: string): SessionState {
    let session = this.sessions.get(sessionId);
    if (!session) {
      session = {
        sessionId,
        consumers: new Map(),
        route: null,
        watch: null,
        workspace: null,
        custom: null,
        established: null,
        problem: null,
        inFlight: null,
        dirty: false,
        refreshQueued: false,
        attempted: false,
        lastFinishedAt: 0,
        slow: false,
        connectTimer: undefined,
        connectTimedOut: false,
        pollTimer: undefined,
        idleTimer: undefined,
        idleTtlTimer: undefined,
        headOverride: false,
        accessEpoch: 0,
        lastPolicyAskAt: Number.NEGATIVE_INFINITY,
        recoveryUsed: new Map(),
        listSerial: 0,
        listsFinished: 0,
        recoveryWaiters: [],
        listeners: new Set(),
        snapshot: undefined as unknown as ComparisonOwnerSnapshot,
      };
      session.snapshot = this.snapshotOf(session);
      this.sessions.set(sessionId, session);
    }
    return session;
  }

  private enabledConsumers(session: SessionState): ComparisonConsumer[] {
    return [...session.consumers.values()].filter((consumer) => consumer.enabled);
  }

  private active(session: SessionState): boolean {
    return this.enabledConsumers(session).length > 0;
  }

  /** Whether the session shows a comparison its host lists: a typed one, or the branch point. */
  private wantsHostComparison(session: SessionState): boolean {
    return session.custom !== null || (this.intent !== "head" && !session.headOverride);
  }

  /** Whether the comparison this session lists, typed or the branch point, can be made now. */
  private support(session: SessionState): BranchPointSupport {
    return session.custom ? this.revisionSupportOf(session) : this.branchPointSupportOf(session);
  }

  private revisionSupportOf(session: SessionState): BranchPointSupport {
    // A typed comparison belongs to one workspace, so none is made before it's known.
    if (!session.workspace) return { kind: "checking" };
    return this.hostKnownSupport(session, revisionSupport({ route: session.route }));
  }

  private branchPointSupportOf(session: SessionState): BranchPointSupport {
    return this.hostKnownSupport(session, branchPointSupport({ route: session.route }));
  }

  private hostKnownSupport(session: SessionState, support: BranchPointSupport): BranchPointSupport {
    // A session's comparisons are asked of its host only once the host is known.
    const hostKnown = [...session.consumers.values()].some(
      (consumer) => consumer.sessionHostId !== null,
    );
    return support.kind === "available" && !hostKnown ? { kind: "checking" } : support;
  }

  private attention(session: SessionState): void {
    if (!this.deps.documentVisible()) return;
    // A host that couldn't be asked may answer now; ask again, briefly bounded.
    const now = this.deps.now();
    if (
      session.route?.state === "unreachable" &&
      now - session.lastPolicyAskAt >= ATTENTION_MIN_INTERVAL_MS
    ) {
      session.lastPolicyAskAt = now;
      session.watch?.ask();
    }
    // A recent list stands, but the poll a hidden window dropped starts again.
    if (this.deps.now() - session.lastFinishedAt >= ATTENTION_MIN_INTERVAL_MS) {
      session.dirty = true;
    }
    this.pump(session);
  }

  private readCustom(session: SessionState): RevisionSpec | null {
    if (!session.workspace) return null;
    return this.deps.customComparisons?.read(session.sessionId, session.workspace) ?? null;
  }

  private clearCustom(session: SessionState): void {
    if (session.workspace) {
      this.deps.customComparisons?.clear(session.sessionId, session.workspace);
    }
    this.applyCustom(session, null);
  }

  /**
   * Follows the workspace the session's views report. A session moved to
   * another host or root shows that workspace's typed comparison, if any, and
   * nothing listed for the previous one. An unknown root keeps the last one.
   */
  private syncWorkspace(session: SessionState): void {
    const reporting = [...session.consumers.values()].find((consumer) => !!consumer.root);
    if (!reporting?.root) return;
    const next = { hostId: reporting.sessionHostId, root: reporting.root };
    const current = session.workspace;
    if (current && current.hostId === next.hostId && current.root === next.root) return;
    session.workspace = next;
    if (!current) {
      this.applyCustom(session, this.readCustom(session));
      return;
    }
    // Whatever was listed, recovered or chosen came from the previous workspace, even under the same names.
    session.custom = this.readCustom(session);
    this.deps.forgetListings?.(session.sessionId);
    this.dropComparison(session);
  }

  /**
   * Switches the session to a typed comparison, or back to its saved base.
   * What was listed for the previous one is dropped, along with reads under it.
   */
  private applyCustom(session: SessionState, custom: RevisionSpec | null): void {
    if (sameRevisions(session.custom, custom)) return;
    session.custom = custom;
    this.dropComparison(session);
  }

  /**
   * Drops the session's comparison and everything tied to it: the list in
   * flight, its recoveries, which settle false, and the session's choices.
   */
  private dropComparison(session: SessionState): void {
    this.abortList(session);
    session.established = null;
    session.problem = null;
    session.attempted = false;
    session.dirty = true;
    session.headOverride = false;
    this.clearChoiceState(session);
    this.settleRecoveries(session, Number.POSITIVE_INFINITY, false);
    this.publish(session);
    this.pump(session);
  }

  private clearChoiceState(session: SessionState): void {
    session.headOverride = false;
    session.connectTimedOut = false;
    session.recoveryUsed.clear();
    this.deps.clearTimeout(session.connectTimer);
    session.connectTimer = undefined;
    this.publish(session);
  }

  private pump(session: SessionState): void {
    const consumers = this.enabledConsumers(session);
    if (consumers.length === 0) {
      this.idle(session);
      return;
    }
    this.ensureRoute(session);
    if (!this.wantsHostComparison(session)) {
      this.abortList(session);
      this.deps.clearTimeout(session.pollTimer);
      session.pollTimer = undefined;
      return;
    }
    this.ensureConnectClock(session);
    if (this.support(session).kind !== "available") return;
    if (session.inFlight) return;
    if (!session.attempted || session.dirty) {
      this.startList(session);
      return;
    }
    this.schedulePoll(session, consumers);
  }

  /** Nobody may read this workspace now: stop work, then forget it if nobody returns. */
  private idle(session: SessionState): void {
    this.abortList(session);
    for (const timer of [session.pollTimer, session.connectTimer]) this.deps.clearTimeout(timer);
    session.pollTimer = undefined;
    session.connectTimer = undefined;
    if (session.idleTimer !== undefined || session.idleTtlTimer !== undefined) return;
    if (!session.watch && !session.established && !session.headOverride) return;
    session.idleTimer = this.deps.setTimeout(() => {
      session.idleTimer = undefined;
      if (this.active(session)) return;
      session.watch?.stop();
      session.watch = null;
      session.idleTtlTimer = this.deps.setTimeout(() => {
        session.idleTtlTimer = undefined;
        if (this.active(session)) return;
        this.forget(session);
        this.publish(session);
      }, IDLE_TTL_MS - IDLE_GRACE_MS);
    }, IDLE_GRACE_MS);
  }

  private clearIdleTimers(session: SessionState): void {
    this.deps.clearTimeout(session.idleTimer);
    this.deps.clearTimeout(session.idleTtlTimer);
    session.idleTimer = undefined;
    session.idleTtlTimer = undefined;
  }

  /** Drops what the session learned and what the person chose for it. */
  private forget(session: SessionState): void {
    session.established = null;
    session.problem = null;
    session.attempted = false;
    session.dirty = false;
    session.refreshQueued = false;
    session.lastFinishedAt = 0;
    session.slow = false;
    session.connectTimedOut = false;
    session.headOverride = false;
    session.recoveryUsed.clear();
    this.settleRecoveries(session, Number.POSITIVE_INFINITY, false);
  }

  /** Answers the recoveries waiting on a list numbered below `finished`. */
  private settleRecoveries(session: SessionState, finished: number, relisted: boolean): void {
    const waiting = session.recoveryWaiters;
    session.recoveryWaiters = waiting.filter((waiter) => waiter.after >= finished);
    for (const waiter of waiting) if (waiter.after < finished) waiter.resolve(relisted);
  }

  private ensureRoute(session: SessionState): void {
    if (session.watch) return;
    const generation = this.generation;
    // Declared first: a watch may answer before it returns.
    const watchState: NonNullable<SessionState["watch"]> = { stop: () => {}, ask: () => {} };
    session.watch = watchState;
    const watch = this.deps.watchRoute({ sessionId: session.sessionId }, (route) => {
      if (generation !== this.generation || session.watch !== watchState) return;
      const wasAvailable = this.support(session).kind === "available";
      session.route = route;
      const support = this.support(session);
      // A comparison can't outlive its host no longer serving them.
      if (support.kind === "unsupported") {
        session.established = null;
        this.abortList(session);
      }
      if (!wasAvailable && support.kind === "available") session.dirty = true;
      this.publish(session);
      this.pump(session);
    });
    watchState.stop = watch.stop;
    watchState.ask = watch.ask;
  }

  private ensureConnectClock(session: SessionState): void {
    if (session.established || session.connectTimedOut || session.connectTimer !== undefined) {
      return;
    }
    // Only Automatic falls back to the last commit; a typed comparison never does.
    if (this.intent !== "automatic" || session.custom) return;
    const generation = this.generation;
    session.connectTimer = this.deps.setTimeout(() => {
      session.connectTimer = undefined;
      if (generation !== this.generation || session.established) return;
      session.connectTimedOut = true;
      this.publish(session);
    }, CONNECT_FALLBACK_MS);
  }

  private schedulePoll(session: SessionState, consumers: ComparisonConsumer[]): void {
    this.deps.clearTimeout(session.pollTimer);
    session.pollTimer = undefined;
    const surfaced = consumers.some((consumer) => consumer.kind === "surface");
    if (!surfaced || !this.deps.documentVisible()) return;
    const interval = session.slow ? SLOW_POLL_MS : POLL_MS;
    const due = Math.max(0, session.lastFinishedAt + interval - this.deps.now());
    session.pollTimer = this.deps.setTimeout(() => {
      session.pollTimer = undefined;
      if (!this.deps.documentVisible()) return;
      session.dirty = true;
      this.pump(session);
    }, due);
  }

  private startList(session: SessionState): void {
    this.deps.clearTimeout(session.pollTimer);
    session.pollTimer = undefined;
    const controller = new AbortController();
    const generation = this.generation;
    const refresh = session.refreshQueued;
    const spec = session.custom;
    session.inFlight = controller;
    session.dirty = false;
    session.refreshQueued = false;
    session.attempted = true;
    session.listSerial += 1;
    const serial = session.listSerial;
    const startedAt = this.deps.now();
    this.publish(session);
    const current = () => generation === this.generation && session.inFlight === controller;
    void this.deps
      .fetchListing(session.sessionId, { refresh, signal: controller.signal, spec })
      .then(
        (listing) => {
          if (current()) this.accept(session, listing);
        },
        (error: unknown) => {
          if (!current() || controller.signal.aborted) return;
          this.accept(session, { kind: "unavailable", problem: listingProblemFrom(error) });
          // The host's answer about the session may have changed with it.
          session.watch?.ask();
        },
      )
      .finally(() => {
        if (!current()) return;
        session.inFlight = null;
        session.lastFinishedAt = this.deps.now();
        session.slow =
          session.lastFinishedAt - startedAt > SLOW_LIST_MS ||
          SLOW_PROBLEMS.has(session.problem?.reason ?? "");
        session.listsFinished += 1;
        this.publish(session);
        this.settleRecoveries(session, serial, true);
        this.pump(session);
      });
  }

  private accept(session: SessionState, listing: BranchPointListing): void {
    if (listing.kind === "comparison") {
      session.established = listing.comparison;
      session.problem = null;
      session.connectTimedOut = false;
      this.deps.clearTimeout(session.connectTimer);
      session.connectTimer = undefined;
      return;
    }
    session.problem = listing.problem;
    // A definitive answer ends the comparison; a transient one keeps it on screen.
    if (!listing.problem.transient) session.established = null;
  }

  private abortList(session: SessionState): void {
    if (!session.inFlight) return;
    session.inFlight.abort();
    session.inFlight = null;
    // The aborted list didn't happen; whatever asked for it still wants one.
    session.dirty = true;
  }

  private stopTimers(session: SessionState): void {
    for (const timer of [session.pollTimer, session.connectTimer]) this.deps.clearTimeout(timer);
    session.pollTimer = undefined;
    session.connectTimer = undefined;
    this.clearIdleTimers(session);
  }

  private snapshotOf(session: SessionState): ComparisonOwnerSnapshot {
    return {
      generation: this.generation,
      support: this.branchPointSupportOf(session),
      revisionSupport: this.revisionSupportOf(session),
      custom: session.custom,
      workspace: session.workspace,
      established: session.established,
      problem: session.problem,
      listing: session.inFlight !== null,
      connectTimedOut: session.connectTimedOut,
      headOverride: session.headOverride,
      accessEpoch: session.accessEpoch,
    };
  }

  private publish(session: SessionState): void {
    const next = this.snapshotOf(session);
    const previous = session.snapshot;
    if (
      previous &&
      previous.generation === next.generation &&
      previous.established === next.established &&
      previous.problem === next.problem &&
      previous.listing === next.listing &&
      previous.connectTimedOut === next.connectTimedOut &&
      previous.headOverride === next.headOverride &&
      previous.accessEpoch === next.accessEpoch &&
      sameRevisions(previous.custom, next.custom) &&
      previous.workspace === next.workspace &&
      JSON.stringify(previous.support) === JSON.stringify(next.support) &&
      JSON.stringify(previous.revisionSupport) === JSON.stringify(next.revisionSupport)
    ) {
      return;
    }
    session.snapshot = next;
    for (const listener of session.listeners) listener();
  }
}
