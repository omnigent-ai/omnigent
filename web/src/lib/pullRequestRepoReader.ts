// Who a pull request's repository is read and changed as on this Mac: its saved login where the
// shell can read as it, nobody where it can't, and the usual account where nothing is chosen.
// Installed into the read path, which routes every read and change of the PR by it.

import {
  installRepoReaderPolicy,
  readChoiceFor,
  repoReaderPolicyChanged,
} from "@/lib/desktopReadPath";
import { readChoiceKey } from "@/lib/pullRequestReadRoute";
import { readPathOwnerMoved } from "@/lib/readPathOwner";
import {
  leaveNamedReader,
  namedReadsAvailable,
  namedReadsFlagOn,
  namedReadsSettled,
  sameNamedReader,
  watchNamedReads,
  type MacNamedReader,
} from "@/lib/macNamedReads";
import {
  NO_REPO_READER,
  pullRequestRepository,
  savedRepoAccount,
  subscribeRepoAccounts,
  type PullRequestRepository,
  type RepoReaderState,
} from "@/lib/pullRequestRepoAccounts";

/** The repository's reader from its saved choice and what the shell can do now. */
export function repoReaderState(url: string): RepoReaderState {
  const repository = pullRequestRepository(url);
  // Choices are applied only for the page's first owner; after a move the host reads the PR.
  if (!repository || readPathOwnerMoved()) return NO_REPO_READER;
  // "Session's machine", chosen for this PR, reads it there on purpose, with no Mac account.
  if (readChoiceFor(url) === "session") return NO_REPO_READER;
  return stateFor(repository);
}

function stateFor(repository: PullRequestRepository): RepoReaderState {
  const saved = savedRepoAccount(repository);
  if (saved.kind === "pending") return { kind: "blocked", reason: "pending", reader: null };
  if (saved.intent.mode !== "account") return NO_REPO_READER;
  const reader: MacNamedReader = { host: repository.host, login: saved.intent.login };
  if (!namedReadsSettled()) return { kind: "blocked", reason: "checking", reader };
  if (!namedReadsAvailable()) {
    return { kind: "blocked", reason: namedReadsFlagOn() ? "update_needed" : "turned_off", reader };
  }
  return { kind: "named", reader };
}

// Per pull request this run read as a repository's login: that login, so a change of choice,
// user, shell or the PR's own read source retires it before anything reads again.
const named = new Map<string, MacNamedReader>();
let availability: string | null = null;
let installed = false;
// Ends what `follow` listens to; null while nothing is followed.
let stopFollowing: (() => void) | null = null;

/**
 * Follow choices and named reads from the first route asked. Named reads aren't started here:
 * only a repository with a saved login asks for them.
 */
function follow(): void {
  if (stopFollowing) return;
  const stopChoices = subscribeRepoAccounts(choicesChanged);
  const stopNamed = watchNamedReads(namedReadsChanged);
  stopFollowing = () => {
    stopChoices();
    stopNamed();
  };
}

const availabilityNow = () =>
  `${namedReadsSettled()}:${namedReadsAvailable()}:${namedReadsFlagOn()}`;

function readerFor(url: string): RepoReaderState {
  follow();
  const state = repoReaderState(url);
  // Kept under the PR's choice key, so a change of its read source finds it however it's spelled.
  if (state.kind === "named") named.set(readChoiceKey(url) ?? url, state.reader);
  return state;
}

/**
 * Retire the login each of `urls` read as where it no longer does, so its reads still in flight
 * are refused even if it comes back. The login's other views read again once too: they share
 * its scope, and its answers can't be told apart by the view that asked.
 */
function retireLeft(urls: Iterable<string>): void {
  const left: MacNamedReader[] = [];
  for (const url of urls) {
    const reader = named.get(url);
    if (!reader) continue;
    const next = repoReaderState(url);
    if (next.kind === "named" && sameNamedReader(next.reader, reader)) continue;
    if (next.kind === "named") named.set(url, next.reader);
    else named.delete(url);
    if (!left.some((known) => sameNamedReader(known, reader))) left.push(reader);
  }
  for (const reader of left) leaveNamedReader(reader);
}

/** A choice, the workspace user, or the shell changed: every PR read by name is checked. */
function choicesChanged(): void {
  retireLeft([...named.keys()]);
  repoReaderPolicyChanged();
}

/** The PR's own read source changed, e.g. to Session's machine: its login retires if it left. */
function readChoiceChanged(url: string): void {
  retireLeft([url]);
}

/** Re-route only when whether named reads work changed, not on every reader's epoch. */
function namedReadsChanged(): void {
  const next = availabilityNow();
  if (next === availability) return;
  availability = next;
  choicesChanged();
}

/** Route the read path by repository choices; once per run. */
export function installRepoReader(): void {
  if (installed) return;
  installed = true;
  installRepoReaderPolicy({ readerFor, readChoiceChanged });
}

installRepoReader();

/** Forget what was named, so a test starts clean; installs again on the next call. */
export function resetRepoReaderForTests(): void {
  stopFollowing?.();
  stopFollowing = null;
  named.clear();
  availability = null;
  installed = false;
}
