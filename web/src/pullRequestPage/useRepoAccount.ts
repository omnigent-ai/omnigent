// The live view of which of this Mac's accounts a pull request's repository is read as: the
// saved choice, who it reads as now, and the choices to offer. Shared by the menu and ⌘K.

import { useMemo } from "react";

import { showToast } from "@/components/ui/toast";
import { useMacAccountChoice } from "@/hooks/useMacAccounts";
import { macIdentity, repoReaderFor, useDesktopReadPathVersion } from "@/lib/desktopReadPath";
import {
  namedWriterIdentity,
  useNamedReadsAvailable,
  useNamedReadsVersion,
} from "@/lib/macNamedReads";
import {
  pullRequestRepository,
  savedRepoAccount,
  setRepoAccount,
  type PullRequestRepository,
  type RepoAccountIntent,
  type RepoReaderState,
} from "@/lib/pullRequestRepoAccounts";
// Installs the repository choices' policy before anything shows them.
import "@/lib/pullRequestRepoReader";

import { repoAccountChoices, type RepoAccountChoice } from "./repoAccountChoices";

export const REPO_ACCOUNT_SAVE_FAILED = "Couldn’t remember the account for this repository.";

export interface RepoAccountView {
  repository: PullRequestRepository;
  intent: RepoAccountIntent;
  state: RepoReaderState;
  /** The chosen login is read as, but main refused it: signed out or expired. */
  signedOut: boolean;
  choices: RepoAccountChoice[];
  choose: (intent: RepoAccountIntent) => void;
}

/** Save `intent` for `repository`; says so where it couldn't be kept. */
export function chooseRepoAccount(
  repository: PullRequestRepository,
  intent: RepoAccountIntent,
): boolean {
  const saved = setRepoAccount(repository, intent);
  if (!saved) showToast(REPO_ACCOUNT_SAVE_FAILED);
  return saved;
}

/**
 * The repository's account view, or null where there's nothing to offer: not a github.com PR,
 * or this Mac can't read as a named login and none is chosen. A chosen login stays offered,
 * even while it can't be used, so the user can always move off it.
 */
export function useRepoAccount(prUrl: string | null | undefined): RepoAccountView | null {
  const routeVersion = useDesktopReadPathVersion();
  const namedVersion = useNamedReadsVersion();
  const available = useNamedReadsAvailable();
  const repository = pullRequestRepository(prUrl);
  // The same query the host-wide account rows read, so asking for it costs nothing more.
  const choice = useMacAccountChoice(repository?.host ?? null);
  const host = repository?.host ?? null;
  const owner = repository?.owner ?? null;
  const repo = repository?.repo ?? null;
  // Rebuilt only when what it shows changes, so ⌘K's rows don't churn on every render.
  return useMemo(() => {
    // The versions stand for the stores read below: the route, named reads and the choices.
    void routeVersion;
    void namedVersion;
    if (!host || !owner || !repo || !prUrl) return null;
    const picked: PullRequestRepository = { host, owner, repo };
    const saved = savedRepoAccount(picked);
    const intent: RepoAccountIntent = saved.kind === "saved" ? saved.intent : { mode: "automatic" };
    if (!available && intent.mode === "automatic") return null;
    const state = repoReaderFor(prUrl);
    const automaticLogin = choice?.effective?.login ?? macIdentity(host)?.login ?? null;
    return {
      repository: picked,
      intent,
      state,
      signedOut: state.kind === "named" && namedWriterIdentity(state.reader) === "refused",
      choices: repoAccountChoices({
        repository: picked,
        intent,
        accounts: choice?.accounts ?? [],
        automaticLogin,
      }),
      choose: (next) => void chooseRepoAccount(picked, next),
    };
  }, [host, owner, repo, prUrl, available, choice, routeVersion, namedVersion]);
}
