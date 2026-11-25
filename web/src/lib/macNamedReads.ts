// Reads the Pull Requests page runs as one of this Mac's GitHub logins it names itself,
// beside the host's own account, which they never change. Each host and login has its own
// scope: answers are ordered and cached under it, kept only when main and GitHub both say
// they were read as that login, and dropped when it signs out or the page reads as another.

import { useSyncExternalStore } from "react";

import { subscribeDesktopFeatureFlag } from "@/lib/desktopFeatureFlags";
import {
  desktopReadSupportKnown,
  macNamedReadsOffered,
  refreshDesktopReadSupport,
  useDesktopReadPathVersion,
} from "@/lib/desktopReadPath";
import { getCurrentUserId } from "@/lib/identity";
import {
  acceptMacRead,
  acceptedMacReader,
  advanceMacReadScope,
  MAC_REFUSED_CODES,
  refuseMacReadScope,
  beginMacRead,
  macNamedReadScopeKey,
  macReadCurrent,
  type MacNamedReadScopeKey,
  type MacReadTicket,
} from "@/lib/macReadScope";
import {
  hasCodeHostAccountsBridge,
  hasCodeHostBridge,
  hasNamedAccountEvents,
  onDesktopNamedAccountsChanged,
} from "@/lib/nativeBridge";

/** The release flag for reading as a named login; main checks it on every read too. */
export const PULL_REQUESTS_PAGE_ACCOUNTS_FLAG = "pull-requests-page-accounts";

/**
 * The reads main runs as a named login: the page's list and what its pull request shows.
 * Write options and Check Again join them where the shell writes as that login too
 * ({@link NAMED_WRITE_READS}); merge readiness stays with the host's own account.
 */
export const NAMED_READ_OPERATIONS: ReadonlySet<string> = new Set([
  "pull_request_inbox",
  "pull_request_search",
  // A list's later pages, as the login its first page answered as.
  "pull_request_search_page",
  // A Markdown editor's `#` completion, as the account its pull request is read as.
  "pull_request_completion_search",
  "pull_request_overview",
  "pull_request_list_stats",
  "pull_request_files",
  "pull_request_patch",
  "pull_request_file_contents",
  "review_threads",
  "review_thread_comments",
  "pull_request_commits",
  "commit_diff",
  "pull_request_images",
  "pull_request_stack_members",
  "pull_request_stack_compare",
  "pull_request_native_stack",
]);

/**
 * The reads a change as a named login needs, its controls' options and the checks that
 * settle it: run as that login only where the shell also writes as it (`writeAs`).
 */
export const NAMED_WRITE_READS: ReadonlySet<string> = new Set([
  "pull_request_label_options",
  "pull_request_label_search",
  "pull_request_label_lookup",
  "pull_request_review_candidates",
  "pull_request_merge_options",
  "pull_request_comment_state",
  "pull_request_comment_reactions",
  "pull_request_review_check",
  "pull_request_pending_review",
  "pull_request_review_state",
]);

/** One of this Mac's GitHub logins a page reads as by name. */
export interface MacNamedReader {
  host: string;
  login: string;
}

/** What a named read captured when it was sent, to check its answer against. */
export interface NamedAnswerTicket {
  reader: MacNamedReader;
  userId: string | null;
  read: MacReadTicket;
}

const sameLogin = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();

export const sameNamedReader = (
  left: MacNamedReader | null | undefined,
  right: MacNamedReader | null | undefined,
) =>
  left === right ||
  (!!left &&
    !!right &&
    left.host.toLowerCase() === right.host.toLowerCase() &&
    sameLogin(left.login, right.login));

/** The scope `reader`'s answers are cached under, for `userId` (the current workspace user). */
export function namedReadScope(
  reader: MacNamedReader,
  userId: string | null = getCurrentUserId(),
): MacNamedReadScopeKey {
  return macNamedReadScopeKey(reader.host, reader.login, userId);
}

const scopeId = (host: string, login: string) => `${host.toLowerCase()}\n${login.toLowerCase()}`;
// Per host and login: the oldest generation main may still answer with, from its events.
const floors = new Map<string, number>();
// Every reader a read was sent as this run, so a rollback can drop each one's scope.
const seen = new Set<string>();
// Per host and login: how often what was read as it had to go, for copies kept outside queries.
const epochs = new Map<string, number>();
// Per host and login: the identity its last kept answer was read as, the one a change as it
// names, until its status changes; and the logins main refused to read as since.
const writers = new Map<string, { login: string; generation: number }>();
const refusedReaders = new Set<string>();
const listeners = new Set<() => void>();
let flagOn = false;
// Each flag answer is numbered; a saved choice waits until the latest one has settled, i.e.
// the shell was asked again what it supports under it, so it never reads on a stale answer.
let flagAnswers = 0;
let settledAnswer = 0;
let version = 0;
let started = false;
/** Ends what `start` listens to, so a test's reset leaves no subscription behind. */
let stopWatching: (() => void) | null = null;

function changed(): void {
  version += 1;
  for (const listener of listeners) listener();
}

/**
 * Refuse every read of `reader` sent before now and drop what it cached; on `left`, what this
 * device kept for it stays, since the login itself didn't change.
 */
function advance(host: string, login: string, move: "advanced" | "left" = "advanced"): void {
  writers.delete(scopeId(host, login));
  bumpEpoch(host, login);
  advanceMacReadScope(macNamedReadScopeKey(host, login), move);
  changed();
}

function bumpEpoch(host: string, login: string): void {
  const id = scopeId(host, login);
  epochs.set(id, (epochs.get(id) ?? 0) + 1);
}

/** Moves whenever what was read as `reader` must go, e.g. it signed out or the page left it. */
export function namedReaderEpoch(reader: MacNamedReader): number {
  return epochs.get(scopeId(reader.host, reader.login)) ?? 0;
}

/** Call `listener` whenever named reads become available or not, or a reader's answers go. */
export function subscribeNamedReads(listener: () => void): () => void {
  return subscribe(listener);
}

function start(): void {
  if (started) return;
  started = true;
  // A login signing out, expiring or signing in again: what was read as it goes.
  const stopAccounts = onDesktopNamedAccountsChanged((host, login, generation) => {
    const id = scopeId(host, login);
    if (generation !== null) floors.set(id, Math.max(floors.get(id) ?? 0, generation));
    advance(host, login);
  });
  const stopFlag = subscribeDesktopFeatureFlag(
    PULL_REQUESTS_PAGE_ACCOUNTS_FLAG,
    { fallback: false, mode: "resolved" },
    ({ value }) => {
      const next = value === true;
      const answer = ++flagAnswers;
      // Main advertises `readAs` only while its flag is on: an answer turning it on settles
      // once the shell has been asked again; a later answer supersedes an earlier one's ask.
      const settle = () => {
        if (answer !== flagAnswers || settledAnswer === answer) return;
        settledAnswer = answer;
        changed();
      };
      if (next === flagOn) {
        if (next) void refreshDesktopReadSupport().finally(settle);
        else settle();
        changed();
        return;
      }
      flagOn = next;
      // A rollback drops every named answer at once; main refuses those still in flight.
      if (!next) {
        for (const id of new Set([...floors.keys(), ...seen])) {
          const [host, login] = id.split("\n") as [string, string];
          advance(host, login);
        }
      }
      // Off needs nothing more from the shell: the page reads as the Mac's account.
      if (next) void refreshDesktopReadSupport().finally(settle);
      else settle();
      changed();
    },
  );
  stopWatching = () => {
    stopFlag();
    stopAccounts();
  };
}

/**
 * Whether pages may read as a named login now: the flag, and a shell with the whole contract,
 * so a saved choice never reads as a login without a way to see or change it. That's reads
 * (`codeHostQuery`), this Mac's accounts (`codeHostAccounts` get and set, listing who can be
 * named and who acts), `readAs`, and the event saying when such a login signs out.
 */
export function namedReadsAvailable(): boolean {
  start();
  return (
    flagOn &&
    hasCodeHostBridge() &&
    hasCodeHostAccountsBridge() &&
    hasNamedAccountEvents() &&
    macNamedReadsOffered()
  );
}

/**
 * Call `listener` like {@link subscribeNamedReads}, without starting to follow the flag and the
 * shell: for a store that only needs to hear changes once something else asked.
 */
export function watchNamedReads(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Whether the release flag for named reads is on, as last answered; false before it answers. */
export function namedReadsFlagOn(): boolean {
  start();
  return flagOn;
}

function subscribe(listener: () => void): () => void {
  start();
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Moves whenever named reads become available or not, or a reader's answers must go. */
export function useNamedReadsVersion(): number {
  return useSyncExternalStore(
    subscribe,
    () => version,
    () => 0,
  );
}

/** {@link namedReadsAvailable}, re-rendering when the flag, the shell or a reader changes. */
export function useNamedReadsAvailable(): boolean {
  useDesktopReadPathVersion();
  useSyncExternalStore(
    subscribe,
    () => version,
    () => 0,
  );
  return namedReadsAvailable();
}

/**
 * Whether it's known yet if named reads are available: the flag answered and the shell said
 * what it supports. Older shells and browsers settle at once, on today's behavior.
 */
export function namedReadsSettled(): boolean {
  start();
  return flagAnswers > 0 && settledAnswer === flagAnswers && desktopReadSupportKnown();
}

/** Number a read as `reader` before it's sent. */
export function namedReadTicket(reader: MacNamedReader): NamedAnswerTicket {
  start();
  seen.add(scopeId(reader.host, reader.login));
  const key = namedReadScope(reader);
  return { reader, userId: key[1], read: beginMacRead(key) };
}

/**
 * Whether a named answer may be kept: for the same workspace user, while named reads are
 * still available, not predating a change of the login's status, and read as that login by
 * main's account and GitHub's viewer both.
 */
export function acceptNamedAnswer(
  ticket: NamedAnswerTicket,
  answer: { identity?: unknown; provenance?: unknown },
): "accepted" | "moved" | "unproven" {
  const { host, login } = ticket.reader;
  if (ticket.userId !== getCurrentUserId() || !macReadCurrent(ticket.read)) return "moved";
  // Rolled back, or the shell stopped offering it, while this read was out.
  if (!namedReadsAvailable()) return "moved";
  // The bridge's envelope is untyped: anything short of a whole, matching one is refused.
  const identity = (answer.identity ?? null) as Record<string, unknown> | null;
  const provenance = (answer.provenance ?? null) as Record<string, unknown> | null;
  const text = (value: unknown): value is string => typeof value === "string" && value.length > 0;
  const generation = identity?.generation;
  const readAs =
    identity !== null &&
    identity.named === true &&
    text(identity.host) &&
    text(identity.login) &&
    Number.isSafeInteger(generation) &&
    (generation as number) >= 0 &&
    identity.host.toLowerCase() === host.toLowerCase() &&
    sameLogin(identity.login, login) &&
    provenance !== null &&
    provenance.version === 1 &&
    provenance.provider === "github" &&
    text(provenance.host) &&
    text(provenance.viewer) &&
    provenance.host.toLowerCase() === host.toLowerCase() &&
    sameLogin(provenance.viewer, login);
  if (!readAs) return "unproven";
  if ((generation as number) < (floors.get(scopeId(host, login)) ?? 0)) {
    advance(host, login);
    return "moved";
  }
  if (acceptMacRead(ticket.read, identity.login as string) === "rejected") return "moved";
  const id = scopeId(host, login);
  const known = writers.get(id);
  refusedReaders.delete(id);
  if (!known || known.generation !== generation || known.login !== identity.login) {
    writers.set(id, { login: identity.login as string, generation: generation as number });
    changed();
  }
  return "accepted";
}

/**
 * Whether an answer read on another host *for* `ticket`'s named login may show: for the same
 * workspace user, while named reads are on, the ticket current, and `reader` (the login main
 * read it for) that login at a generation from no earlier than its last change. The other
 * host proves its own reader.
 */
/**
 * Per named login, the newest generation an answer read for it on another host was kept at, so
 * a late one from before a change can't replace it. Never makes the login a writer.
 */
const answeredFor = new Map<string, number>();

export function acceptNamedReaderFor(
  ticket: NamedAnswerTicket,
  reader: { login: string; generation: number; named?: true },
): "accepted" | "moved" {
  const { host, login } = ticket.reader;
  if (ticket.userId !== getCurrentUserId() || !macReadCurrent(ticket.read)) return "moved";
  if (!namedReadsAvailable() || reader.named !== true || !sameLogin(reader.login, login)) {
    return "moved";
  }
  if (!Number.isSafeInteger(reader.generation) || reader.generation < 0) return "moved";
  const id = scopeId(host, login);
  // A refusal or change advanced the scope, so a ticket from before it isn't current above; a
  // fresh one main answered for the login at a current generation recovers it here.
  if (reader.generation < (floors.get(id) ?? 0)) return "moved";
  const known = writers.get(id);
  if (known && known.generation > reader.generation) return "moved";
  if (reader.generation < (answeredFor.get(id) ?? 0)) return "moved";
  answeredFor.set(id, reader.generation);
  return "accepted";
}

/**
 * Who a change as `reader` acts as: the login and generation its last kept answer proved, or
 * whether that's still being read or main refused to read as it. Never another account.
 */
export function namedWriterIdentity(
  reader: MacNamedReader,
): { login: string; generation: number } | "checking" | "refused" {
  const id = scopeId(reader.host, reader.login);
  const known = writers.get(id);
  if (known) return known;
  return refusedReaders.has(id) ? "refused" : "checking";
}

const writtenListeners = new Set<(prUrl: string, userId: string | null) => void>();

/**
 * A change to `prUrl` finished on this Mac, as the host's account: what the page shows of it
 * as its own login is read again as that login. Nothing of the actor's answer is copied in.
 */
export function namedPullRequestWritten(prUrl: string, userId: string | null): void {
  for (const listener of writtenListeners) listener(prUrl, userId);
}

/** Call `listener` whenever a change to a pull request finishes on this Mac. */
export function onNamedPullRequestWritten(
  listener: (prUrl: string, userId: string | null) => void,
): () => void {
  writtenListeners.add(listener);
  return () => writtenListeners.delete(listener);
}

/** Codes main answers when the named login itself can't read anymore. */
export const NAMED_REFUSED_CODES: ReadonlySet<string> = MAC_REFUSED_CODES;

/**
 * Main refused to read as `reader` (signed out or expired), whether or not an event said so:
 * what was read as it goes, saved copies too, while the refusal stays to recover from.
 */
export function namedReaderRefused(reader: MacNamedReader): void {
  const id = scopeId(reader.host, reader.login);
  writers.delete(id);
  refusedReaders.add(id);
  bumpEpoch(reader.host, reader.login);
  refuseMacReadScope(namedReadScope(reader));
  changed();
}

/** Main said the login changed mid-read: drop what was read as it, and read again. */
export function namedReaderChanged(reader: MacNamedReader): void {
  advance(reader.host, reader.login);
}

/**
 * The page stopped reading as `reader`: its reads still in flight are refused, so switching
 * away and back never shows an answer sent before the switch.
 */
export function leaveNamedReader(reader: MacNamedReader): void {
  advance(reader.host, reader.login, "left");
}

/**
 * What a restore of an answer this device kept as `reader` needs to stay valid, or null where
 * nothing may restore: this run already kept a fresh named answer read as `viewer` for that
 * exact scope, main and GitHub both proving it, and the login wasn't refused since. A restore
 * proves nothing itself: it never makes a writer or clears a refusal.
 */
export function namedRestoreProof(
  reader: MacNamedReader,
  viewer: string,
): { epoch: number; userId: string } | null {
  const userId = getCurrentUserId();
  if (!userId || !namedReadsAvailable()) return null;
  const id = scopeId(reader.host, reader.login);
  if (refusedReaders.has(id) || !writers.has(id)) return null;
  if (!sameLogin(viewer, reader.login)) return null;
  const accepted = acceptedMacReader(namedReadScope(reader, userId));
  if (!accepted || !sameLogin(accepted, viewer)) return null;
  return { epoch: namedReaderEpoch(reader), userId };
}

/** Whether `proof` still holds: no leave, refusal, change or user switch since it was taken. */
export function namedRestoreProofHolds(
  reader: MacNamedReader,
  proof: { epoch: number; userId: string },
): boolean {
  return (
    proof.userId === getCurrentUserId() &&
    namedReadsAvailable() &&
    proof.epoch === namedReaderEpoch(reader) &&
    !refusedReaders.has(scopeId(reader.host, reader.login))
  );
}

/** Forget everything, so a test starts clean. */
export function resetMacNamedReadsForTests(): void {
  stopWatching?.();
  stopWatching = null;
  floors.clear();
  answeredFor.clear();
  seen.clear();
  epochs.clear();
  writers.clear();
  refusedReaders.clear();
  listeners.clear();
  flagOn = false;
  flagAnswers = 0;
  settledAnswer = 0;
  version = 0;
  started = false;
}
