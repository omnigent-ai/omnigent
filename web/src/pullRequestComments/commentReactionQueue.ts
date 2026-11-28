// A pull request's reaction clicks, sent one at a time through the session's host. Each queue is
// an object for one scope: this window, one PR, one workspace user and sign-in, and one host
// route. A switch of any of them disposes the queue, and a later return makes a new one.
// Everything a click leads to (its pending pill, GitHub's answer, the reread after a refusal or
// an unconfirmed answer) belongs to its queue, and does nothing once it's disposed.
// Nothing here toasts: the comment shows what GitHub has, or says once that it couldn't read it.

import { useSyncExternalStore } from "react";

import {
  type CommentReaction,
  type CommentReactionsState,
  type ReactionContent,
  withViewerReaction,
} from "@/lib/pullRequestCommentReactions";
import { pullRequestKey } from "@/lib/pullRequestReference";
import type { HostChangeRoute } from "@/pullRequestPage/pullRequestPageRoute";

/**
 * Where an intent is:
 * - `queued`: waiting for the ones before it on this PR;
 * - `sending`: out to GitHub;
 * - `rereading`: refused or unconfirmed, so its comment is read again to show what GitHub has;
 * - `settled`: GitHub has it, shown until the PR's next read does too.
 */
export type ReactionIntentStatus = "queued" | "sending" | "rereading" | "settled";

/** How a sent intent ended, as its write reported it. */
export type ReactionSendOutcome =
  /** GitHub made it, with the comment's reactions when its answer carried them. */
  | { kind: "made"; reactions?: CommentReaction[] }
  | { kind: "refused" }
  | { kind: "deleted" }
  | { kind: "unknown" }
  /** The workspace user changed before the answer, which no longer counts. */
  | { kind: "abandoned" };

/** Who a queue acts as: the workspace user and sign-in, and the host account and route. */
export interface ReactionScope {
  prUrl: string;
  owner: string;
  authGeneration: number;
  /** The account the session's host acts as for the page. */
  actor: { login: string; generation: number };
  host: HostChangeRoute;
}

/** What a click captured to act on GitHub; its queue decides whether any of it still counts. */
export interface ReactionIntentEffects {
  /** Send the intent; `settle` hears how it ended. */
  send: (settle: (outcome: ReactionSendOutcome) => void) => void;
  /** Read the comment's reactions as the queue's account; null when it can't tell. */
  reread: () => Promise<CommentReactionsState | null>;
  /** Read the PR again once nothing of the queue is left to send or settle. */
  refresh: () => Promise<unknown>;
}

export interface ReactionIntent {
  id: number;
  commentId: string;
  content: ReactionContent;
  add: boolean;
  status: ReactionIntentStatus;
  effects: ReactionIntentEffects;
}

/** What one comment shows: GitHub's reactions with the pending ones applied in order. */
export interface DisplayedReactions {
  reactions: CommentReaction[];
  /** Reactions with a click not yet confirmed. */
  pending: ReadonlySet<ReactionContent>;
  /** A refused or unconfirmed click whose comment is being read again. */
  rereading: boolean;
  /** That reread failed, so what the comment shows may not be what GitHub has. */
  unconfirmed: boolean;
}

/** What the queues depend on outside themselves: the app's in use, a test's in tests. */
export interface ReactionEnvironment {
  /** Whether `scope` still acts on its PR: same workspace user and sign-in, and host route. */
  scopeCurrent: (scope: ReactionScope) => boolean;
  /** Hear every change that can retire a scope. */
  subscribe: (listener: () => void) => () => void;
}

const noEnvironment: ReactionEnvironment = {
  scopeCurrent: () => false,
  subscribe: () => () => {},
};
let environment = noEnvironment;

/** Where the queues read their scope and show notices; the app sets it once. */
export function setReactionEnvironment(next: ReactionEnvironment): void {
  if (unwatch) {
    unwatch();
    unwatch = null;
  }
  environment = next;
  rewatch();
}

const unsent = (intent: ReactionIntent) => intent.status !== "settled";

/** The scope as one string: a different host route, user or sign-in is another queue. */
export function reactionScopeKey(scope: ReactionScope): string {
  return [
    scope.owner,
    scope.authGeneration,
    pullRequestKey(scope.prUrl),
    scope.actor.login.toLowerCase(),
    scope.actor.generation,
    `${scope.host.conversationId}/${scope.host.key}`,
  ].join("\n");
}

export class ReactionQueue {
  readonly key: string;
  readonly scope: ReactionScope;
  #disposed = false;
  #intents: ReactionIntent[] = [];
  // GitHub's reactions for a comment from an answer or a reread, shown over the read they were
  // learned against until the PR's next read brings a different one.
  #server = new Map<string, { reactions: CommentReaction[]; over: string | null }>();
  // Comments whose reread failed, until the comment's next click.
  #unconfirmed = new Set<string>();
  #holders = 0;
  // How the last click reads the PR again, for a drain whose clicks have all gone.
  #lastEffects: ReactionIntentEffects | null = null;
  #nextId = 1;

  constructor(scope: ReactionScope) {
    this.scope = scope;
    this.key = reactionScopeKey(scope);
  }

  get disposed(): boolean {
    return this.#disposed;
  }

  /** The login the pills name as "You". */
  get viewerLogin(): string {
    return this.scope.actor.login;
  }

  /** Whether the queue still acts; one whose scope has gone is disposed now. */
  live(): boolean {
    if (this.#disposed) return false;
    if (environment.scopeCurrent(this.scope)) return true;
    this.dispose();
    return false;
  }

  /**
   * Queue a click: add or remove the viewer's `content` on `commentId`. It's sent once the PR's
   * earlier reactions have settled, as this queue's account, unless the queue is disposed first.
   */
  enqueue(
    intent: { commentId: string; content: ReactionContent; add: boolean },
    effects: ReactionIntentEffects,
  ): void {
    if (!this.live()) return;
    const id = this.#nextId;
    this.#nextId += 1;
    this.#unconfirmed.delete(intent.commentId);
    this.#lastEffects = effects;
    this.#intents = [...this.#intents, { ...intent, id, status: "queued", effects }];
    changed();
    this.#pump();
  }

  /** `base` with this queue's intents for `commentId` applied in order. */
  displayed(commentId: string, base: readonly CommentReaction[]): DisplayedReactions {
    let reactions = [...this.#serverOver(commentId, base)];
    const pending = new Set<ReactionContent>();
    let rereading = false;
    for (const intent of this.#intents) {
      if (intent.commentId !== commentId) continue;
      // A rereading click shows the comment as it was until GitHub's answer replaces it.
      if (intent.status === "rereading") {
        rereading = true;
        continue;
      }
      reactions = withViewerReaction(reactions, intent.content, intent.add, this.viewerLogin);
      if (intent.status !== "settled") pending.add(intent.content);
    }
    return { reactions, pending, rereading, unconfirmed: this.#unconfirmed.has(commentId) };
  }

  /** A view shows this queue. */
  hold(): void {
    this.#holders += 1;
  }

  /**
   * A view stops showing it. A view that now acts as someone else ends it once no view shows
   * it; one that unmounted leaves its clicks to finish, then ends it.
   */
  release(switched: boolean): void {
    this.#holders = Math.max(0, this.#holders - 1);
    if (this.#holders > 0) return;
    if (switched || !this.#intents.some(unsent)) this.dispose();
  }

  /** End the queue: its clicks and what it learned go, and its callbacks do nothing. */
  dispose(): void {
    if (this.#disposed) return;
    this.#disposed = true;
    this.#intents = [];
    this.#server.clear();
    this.#unconfirmed.clear();
    if (live.get(this.key) === this) live.delete(this.key);
    rewatch();
    changed();
  }

  /** What GitHub said of `commentId`, while the read shown is still the one it answered. */
  #serverOver(commentId: string, base: readonly CommentReaction[]): readonly CommentReaction[] {
    const learned = this.#server.get(commentId);
    if (!learned) return base;
    const shown = JSON.stringify(base);
    learned.over ??= shown;
    if (learned.over === shown) return learned.reactions;
    this.#server.delete(commentId);
    return base;
  }

  #find(id: number): ReactionIntent | undefined {
    return this.#intents.find((intent) => intent.id === id);
  }

  #setStatus(id: number, status: ReactionIntentStatus): void {
    this.#intents = this.#intents.map((intent) =>
      intent.id === id ? { ...intent, status } : intent,
    );
    changed();
  }

  #remove(predicate: (intent: ReactionIntent) => boolean): void {
    this.#intents = this.#intents.filter((intent) => !predicate(intent));
    changed();
  }

  /**
   * GitHub's `reactions` for `commentId`: they include every click on it settled so far, so
   * those go, and the clicks not yet sent apply on top.
   */
  #learn(commentId: string, reactions: CommentReaction[]): void {
    this.#server.set(commentId, { reactions, over: null });
    this.#remove((each) => each.commentId === commentId && each.status === "settled");
  }

  /** Send the next reaction, unless one is out, being checked, or waiting for Check Again. */
  #pump(): void {
    if (!this.live()) return;
    const busy = this.#intents.some(
      (intent) => intent.status === "sending" || intent.status === "rereading",
    );
    if (busy) return;
    const next = this.#intents.find((intent) => intent.status === "queued");
    if (!next) {
      this.#finishIfDrained();
      return;
    }
    this.#setStatus(next.id, "sending");
    let heard = false;
    next.effects.send((outcome) => {
      if (heard) return;
      heard = true;
      this.#settle(next, outcome);
    });
  }

  #settle(intent: ReactionIntent, outcome: ReactionSendOutcome): void {
    if (outcome.kind === "abandoned") {
      this.dispose();
      return;
    }
    // An answer for a queue that ended, or acts as someone else now, changes nothing.
    if (!this.live() || !this.#find(intent.id)) return;
    switch (outcome.kind) {
      case "made":
        this.#setStatus(intent.id, "settled");
        if (outcome.reactions) this.#learn(intent.commentId, outcome.reactions);
        break;
      case "deleted":
        // The comment is gone, with every reaction meant for it.
        this.#remove((each) => each.commentId === intent.commentId);
        break;
      case "refused":
      case "unknown":
        this.#setStatus(intent.id, "rereading");
        void this.#reread(intent);
        return;
    }
    this.#pump();
  }

  /** Read the comment again as this queue's account, and show what GitHub has. */
  async #reread(intent: ReactionIntent): Promise<void> {
    let found: CommentReactionsState | null;
    try {
      found = await intent.effects.reread();
    } catch {
      found = null;
    }
    if (!this.live() || this.#find(intent.id)?.status !== "rereading") return;
    this.#remove((each) => each.id === intent.id);
    if (found?.state === "present") {
      this.#learn(intent.commentId, found.reactions);
    } else if (found?.state === "absent") {
      this.#remove((each) => each.commentId === intent.commentId);
    } else {
      // What GitHub has is unknown, so later clicks on this comment could undo the wrong thing.
      this.#remove((each) => each.commentId === intent.commentId && unsent(each));
      this.#unconfirmed.add(intent.commentId);
      changed();
    }
    this.#pump();
  }

  /**
   * With nothing left to send or check, read the PR again; the settled reactions show until that
   * read does. A click made meanwhile keeps its own, and those settled before it.
   */
  #finishIfDrained(): void {
    const intents = this.#intents;
    if (intents.some(unsent)) return;
    const settled = new Set(intents.map((intent) => intent.id));
    const done = () => {
      if (this.#disposed || this.#intents.some(unsent)) return;
      this.#remove((intent) => settled.has(intent.id));
      if (this.#holders === 0) this.dispose();
    };
    const effects = this.#lastEffects;
    if (!effects) return;
    void effects.refresh().then(done, done);
  }
}

// The live queues, one per scope; a disposed one is never found again.
const live = new Map<string, ReactionQueue>();
const listeners = new Set<() => void>();
let version = 0;
let unwatch: (() => void) | null = null;

function changed(): void {
  version += 1;
  for (const listener of listeners) listener();
}

/** Retire every queue whose scope no longer acts, whether or not a view shows it. */
function retireStale(): void {
  for (const queue of [...live.values()]) queue.live();
}

/** Watch for retiring changes only while some queue is live. */
function rewatch(): void {
  if (live.size > 0 && !unwatch) unwatch = environment.subscribe(retireStale);
  if (live.size === 0 && unwatch) {
    unwatch();
    unwatch = null;
  }
}

/** The live queue for `scope`, if there is one. */
export function peekReactionQueue(scope: ReactionScope): ReactionQueue | null {
  return live.get(reactionScopeKey(scope)) ?? null;
}

/** The live queue for `scope`, made now if there's none. */
export function reactionQueueFor(scope: ReactionScope): ReactionQueue {
  const key = reactionScopeKey(scope);
  const existing = live.get(key);
  if (existing && !existing.disposed) return existing;
  const queue = new ReactionQueue(scope);
  live.set(key, queue);
  rewatch();
  return queue;
}

/** Re-render on any change to any queue. */
export function useReactionQueueVersion(): number {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => version,
    () => 0,
  );
}

/** Dispose every queue, silently; the environment stays as the app set it. */
export function resetReactionQueuesForTests(): void {
  for (const queue of [...live.values()]) queue.dispose();
  live.clear();
  rewatch();
  changed();
}
