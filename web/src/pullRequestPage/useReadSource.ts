// The new page's live view of where one pull request is read, from the shared read route.

import { useSyncExternalStore } from "react";

import {
  automaticReadRoute,
  chooseReadSource,
  macIdentity,
  macReadCandidate,
  readChoiceFor,
  readRouteFor,
  repoReaderFor,
  useDesktopReadPathVersion,
} from "@/lib/desktopReadPath";
import type { RepoReaderState } from "@/lib/pullRequestRepoAccounts";
import { useMacReadAs } from "@/hooks/macReadAs";
import { subscribeIdentity } from "@/lib/identity";
import { readChoiceKey, type ReadChoice, type ReadRoute } from "@/lib/pullRequestReadRoute";

export interface ReadSource {
  /** Whether there's a choice to offer: this Mac could read the PR, whatever is chosen. */
  offered: boolean;
  choice: ReadChoice;
  route: ReadRoute;
  automaticPick: "mac" | "host";
  provider: string | null;
  /** The GitHub host the Mac would read the PR from, when it's GitHub. */
  host: string | null;
  /**
   * The account this Mac reads the PR as, once known: on the Pull Requests page its own, else
   * the repository's chosen login, else the host's account.
   */
  login: string | null;
  /** Who the PR's repository is read as by its own choice; see {@link RepoReaderState}. */
  repo: RepoReaderState;

  choose: (choice: ReadChoice) => void;
}

export function useReadSource(prUrl: string | null | undefined): ReadSource {
  useDesktopReadPathVersion();
  // Who could read the PR stays the same whichever route is chosen, so going back is offered.
  const candidate = macReadCandidate(prUrl);
  const provider = candidate?.provider ?? null;
  const host = provider === "github" ? candidate!.host : null;
  const readAs = useMacReadAs(host);
  const repo = repoReaderFor(prUrl);
  const repoLogin = repo.kind === "none" ? null : (repo.reader?.login ?? null);
  return {
    // GitSpoke has no other path; a GitHub PR needs this Mac's whole read contract.
    offered: provider === "github",
    choice: readChoiceFor(prUrl),
    route: readRouteFor(prUrl),
    automaticPick: automaticReadRoute(prUrl),
    provider,
    host,
    login: readAs?.login ?? repoLogin ?? (host ? (macIdentity(host)?.login ?? null) : null),
    repo,
    choose: (choice) => {
      if (!prUrl) return;
      chooseReadSource(prUrl, choice);
      // A choice that went through replaces any earlier failure's message.
      setReadSourceFailure(prUrl, null);
    },
  };
}

// The last failed switch per PR, for the status line, until the next change goes through.
const failures = new Map<string, string>();
const failureListeners = new Set<() => void>();
let identityWatched = false;

function failuresChanged(): void {
  for (const listener of failureListeners) listener();
}

function watchIdentity(): void {
  if (identityWatched) return;
  identityWatched = true;
  // A message about the previous workspace user's switch isn't the next one's.
  subscribeIdentity(() => {
    if (failures.size === 0) return;
    failures.clear();
    failuresChanged();
  });
}

export function setReadSourceFailure(prUrl: string, message: string | null): void {
  watchIdentity();
  const key = readChoiceKey(prUrl);
  if (!key || (failures.get(key) ?? null) === message) return;
  if (message) failures.set(key, message);
  else failures.delete(key);
  failuresChanged();
}

export function useReadSourceFailure(prUrl: string | null | undefined): string | null {
  watchIdentity();
  return useSyncExternalStore(
    (listener) => {
      failureListeners.add(listener);
      return () => failureListeners.delete(listener);
    },
    () => {
      const key = readChoiceKey(prUrl);
      return key ? (failures.get(key) ?? null) : null;
    },
  );
}

export function resetReadSourceFailuresForTests(): void {
  failures.clear();
  identityWatched = false;
}
