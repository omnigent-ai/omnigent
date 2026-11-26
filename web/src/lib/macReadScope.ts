// The scope Mac-read answers are cached under, and which reader's answers it
// keeps. A scope is one workspace user's reads of one host. Each read takes a
// sequence number before it's sent; the answer names the login that read it
// (from the same request), and is kept only when it can't predate a change of
// reader. So an answer read as one account never shows after a switch to another,
// even when the switch goes back to the first.

import { useSyncExternalStore } from "react";

import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";

import type { CodeHostProviderId } from "@/lib/codeHosts";

/** GitHub, or a registered named host this Mac reads as its own identity. */
export type MacReadProvider = CodeHostProviderId;

/** The query-key prefix for Mac reads of `host`, as `userId` (the current workspace user). */
export function macReadScopeKey(
  provider: MacReadProvider,
  host: string,
  userId: string | null = getCurrentUserId(),
) {
  return ["mac-read", userId, provider, host.toLowerCase()] as const;
}

/**
 * The query-key prefix for reads of `host` as `login`, one of this Mac's accounts a page named
 * itself. Its own head, so no host-wide key is ever part of it and the two never drop each other.
 */
export function macNamedReadScopeKey(
  host: string,
  login: string,
  userId: string | null = getCurrentUserId(),
) {
  return ["mac-read-as", userId, "github", host.toLowerCase(), login.toLowerCase()] as const;
}

/** A host-wide scope: the account this Mac picks for the host. */
export type MacHostReadScopeKey = ReturnType<typeof macReadScopeKey>;
export type MacNamedReadScopeKey = ReturnType<typeof macNamedReadScopeKey>;
/** Any scope a Mac read is ordered and cached under. */
export type MacReadScopeKey = MacHostReadScopeKey | MacNamedReadScopeKey;

/** A read about to be sent: its scope and its place in that scope's order. */
export interface MacReadTicket {
  scope: string;
  sequence: number;
}

/**
 * How an answer fared: kept as the scope's reader, kept as a new reader (so
 * answers cached before it must go), or refused and never stored.
 */
export type MacReadVerdict = "accepted" | "transition" | "rejected";

interface ScopeState {
  /** The last sequence handed out. */
  sequence: number;
  reader: string | null;
  /** The sequence of the last accepted change of reader. */
  floor: number;
  /** The newest accepted sequence. */
  highWater: number;
  /** The record lifecycle; see {@link macReadEpoch}. */
  epoch: number;
}

const scopes = new Map<string, ScopeState>();
const listeners = new Set<() => void>();
/**
 * How a scope moved: `advanced` refuses every read sent before it, and
 * `changed-reader` refuses those older than the newer reader's answer, which is
 * kept, so reads still in flight settle themselves. `refused`: main refused to read
 * as the scope's account (signed out or expired), so what it showed goes, but nothing
 * is read again on its own. `left`: the page stopped reading as the scope's account,
 * which hasn't changed, so it moves as `advanced` does but what this device kept for it stays.
 */
export type MacReadScopeMove = "advanced" | "changed-reader" | "refused" | "left";

const advanceListeners = new Set<(key: MacReadScopeKey, move: MacReadScopeMove) => void>();
// The workspace user scopes were read for, so a switch moves every one of theirs.
let watchedUser: string | null | undefined;
let stopWatchingUser: (() => void) | null = null;

/** Advance every scope of the user who was signed in, whenever the workspace user changes. */
function watchWorkspaceUser(): void {
  if (stopWatchingUser) return;
  watchedUser = getCurrentUserId();
  stopWatchingUser = subscribeIdentity(() => {
    const previous = watchedUser;
    watchedUser = getCurrentUserId();
    if (previous === watchedUser) return;
    // Including a scope whose only read is still in flight, or one only list stats read.
    for (const scope of [...scopes.keys()]) {
      const key = JSON.parse(scope) as MacReadScopeKey;
      if (key[1] === previous) advanceMacReadScope(key);
    }
  });
}
let version = 0;

const scopeId = (key: MacReadScopeKey) => JSON.stringify(key);

function stateOf(scope: string): ScopeState {
  let state = scopes.get(scope);
  if (!state) {
    state = { sequence: 0, reader: null, floor: 0, highWater: 0, epoch: 0 };
    scopes.set(scope, state);
  }
  return state;
}

/** Number a read of `key` before it's sent; every batch in the scope shares the order. */
export function beginMacRead(key: MacReadScopeKey): MacReadTicket {
  watchWorkspaceUser();
  const scope = scopeId(key);
  const state = stateOf(scope);
  state.sequence += 1;
  return { scope, sequence: state.sequence };
}

/** Decide whether an answer read as `reader` under `ticket` may be kept. */
export function acceptMacRead(
  ticket: MacReadTicket,
  reader: string,
  { restored = false }: { restored?: boolean } = {},
): MacReadVerdict {
  const state = stateOf(ticket.scope);
  const sequence = ticket.sequence;
  // Sent before the latest change of reader: older than it, whoever read it.
  if (sequence < state.floor) return "rejected";
  if (state.reader !== null && reader.toLowerCase() === state.reader.toLowerCase()) {
    // A restore isn't a newer answer from GitHub, so a refusal of a read in flight still counts.
    if (!restored) state.highWater = Math.max(state.highWater, sequence);
    return "accepted";
  }
  // Another reader, sent no later than an answer already kept: a stale switch.
  if (state.reader !== null && sequence <= state.highWater) return "rejected";
  // Only GitHub's answer changes a reader; a restore as another account just doesn't show.
  if (state.reader !== null && restored) return "rejected";
  const first = state.reader === null;
  state.reader = reader;
  // The first reader changes from no one, so reads since the last account change stay acceptable.
  if (!first) {
    state.floor = sequence;
    state.epoch += 1;
  }
  if (!restored) state.highWater = sequence;
  version += 1;
  for (const listener of listeners) listener();
  // The first reader replaces nothing, so nothing cached needs to go.
  if (first) return "accepted";
  // What any reader of the scope cached as the previous reader goes.
  const key = JSON.parse(ticket.scope) as MacReadScopeKey;
  for (const listener of advanceListeners) listener(key, "changed-reader");
  return "transition";
}

/**
 * Whether a read sent under `ticket` still predates no change of reader, for
 * answers that name none (older shells): kept only within the same order.
 */
export function macReadCurrent(ticket: MacReadTicket): boolean {
  return ticket.sequence >= stateOf(ticket.scope).floor;
}

/**
 * The shell said the account behind `key` changed: refuse every read sent before
 * now, forget the scope's reader until a new answer names one, and return the
 * scope's key so the caller can drop its cached answers.
 */
export function advanceMacReadScope(
  key: MacReadScopeKey,
  move: "advanced" | "left" = "advanced",
): MacReadScopeKey {
  const state = stateOf(scopeId(key));
  state.sequence += 1;
  state.floor = state.sequence;
  state.highWater = state.sequence;
  state.reader = null;
  state.epoch += 1;
  version += 1;
  for (const listener of listeners) listener();
  for (const listener of advanceListeners) listener(key, move);
  return key;
}

/**
 * Main refused to read as `key`'s account: refuse every read sent before now and tell
 * listeners to drop what it showed, keeping the refusal itself on screen.
 */
export function refuseMacReadScope(key: MacReadScopeKey): void {
  const state = stateOf(scopeId(key));
  state.sequence += 1;
  state.floor = state.sequence;
  state.highWater = state.sequence;
  // A refusal with no reader revokes no record: the refused read's error stays where it is shown,
  // rather than a new record reading (and being refused) again.
  if (state.reader !== null) state.epoch += 1;
  state.reader = null;
  version += 1;
  for (const listener of listeners) listener();
  for (const listener of advanceListeners) listener(key, "refused");
}

/** Codes main answers when the account a read went out as can't read anymore. */
export const MAC_REFUSED_CODES: ReadonlySet<string> = new Set([
  "account_missing",
  "account_invalid",
  "signed_out",
]);

/**
 * Main refused, with `code`, a read of `key` sent under `ticket`: when that says its account
 * can't read anymore, refuse the scope as {@link refuseMacReadScope} does. Only for the workspace
 * user signed in now and a read sent after the scope's last move and newest kept answer, so a
 * late refusal never revokes a reader proven since. Returns whether the scope was refused.
 */
export function refuseMacReadSentAt(
  key: MacReadScopeKey,
  ticket: MacReadTicket,
  code: string,
): boolean {
  if (!MAC_REFUSED_CODES.has(code)) return false;
  if (!key[1] || key[1] !== getCurrentUserId() || ticket.scope !== scopeId(key)) return false;
  const state = scopes.get(ticket.scope);
  if (!state || ticket.sequence < state.floor || ticket.sequence < state.highWater) return false;
  refuseMacReadScope(key);
  return true;
}

/**
 * Call `listener` with a scope's key whenever it advances or changes reader, so
 * the queries cached under it can be dropped. Returns the unsubscribe.
 */
export function onMacReadScopeAdvanced(
  listener: (key: MacReadScopeKey, move: MacReadScopeMove) => void,
): () => void {
  advanceListeners.add(listener);
  return () => advanceListeners.delete(listener);
}

/** The reader whose answers `key` shows, or null before any answer was kept. */
export function acceptedMacReader(key: MacReadScopeKey): string | null {
  return scopes.get(scopeId(key))?.reader ?? null;
}

/**
 * `key`'s record lifecycle: it moves on every change of reader, advance, leave, and refusal of a
 * reader (a workspace-user switch advances), and not on a scope's first reader. Records carry it
 * in their key, so the same login back after a sign-out never reaches what it kept before.
 */
export function macReadEpoch(key: MacReadScopeKey): number {
  return scopes.get(scopeId(key))?.epoch ?? 0;
}

/** Whose record an answer is published into: a scope, its reader (null before one is proven), and epoch. */
export interface MacRecordOwner {
  scope: MacReadScopeKey;
  reader: string | null;
  epoch: number;
}

/**
 * Whether an answer `viewer` sent under `ticket` may be published into `owner`'s record: for the
 * workspace user signed in now, in the same scope and epoch, sent no earlier than its last move,
 * as the scope's accepted reader and the record's own. Newer answers of other reads don't count,
 * so unlike a refusal, a publication isn't held to the scope's newest answer.
 */
export function macReadPublishable(
  ticket: MacReadTicket,
  viewer: string | null,
  owner: MacRecordOwner,
): boolean {
  if (!owner.scope[1] || owner.scope[1] !== getCurrentUserId()) return false;
  if (ticket.scope !== scopeId(owner.scope)) return false;
  const state = scopes.get(ticket.scope);
  if (!state || state.epoch !== owner.epoch || ticket.sequence < state.floor) return false;
  if (!viewer || !state.reader || viewer.toLowerCase() !== state.reader.toLowerCase()) return false;
  return owner.reader === null || owner.reader.toLowerCase() === viewer.toLowerCase();
}

/** Whether an answer cached as `reader` may show under `key` now. */
export function showsMacReader(key: MacReadScopeKey, reader: string | null | undefined): boolean {
  // No workspace user, no Mac reads to show.
  if (!key[1]) return false;
  const accepted = acceptedMacReader(key);
  return !!reader && !!accepted && reader.toLowerCase() === accepted.toLowerCase();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Re-render when any scope's reader changes; returns the store's version. */
export function useMacReadScopeVersion(): number {
  return useSyncExternalStore(
    subscribe,
    () => version,
    () => 0,
  );
}

/** A refused answer: the read runs again under the scope's current order. */
export class StaleMacReadError extends Error {
  constructor() {
    super("This answer was read before the account on this Mac changed.");
    this.name = "StaleMacReadError";
  }
}

/** Forget every scope, so a test starts clean. */
export function resetMacReadScopesForTests(): void {
  scopes.clear();
  listeners.clear();
  // Advance listeners stay: a query client bound to the scopes outlives a reset.
  version = 0;
  stopWatchingUser?.();
  stopWatchingUser = null;
  watchedUser = undefined;
}
