// What the read menu and ⌘K say about which of this Mac's GitHub accounts a repository's pull
// requests are read and changed as. Pure, so both entry points share labels and checks.

import type { MacAccount } from "@/lib/nativeBridge";
import type {
  PullRequestRepository,
  RepoAccountIntent,
  RepoReaderState,
} from "@/lib/pullRequestRepoAccounts";

/** The menu group's heading. */
export const REPO_ACCOUNT_HEADING = "Account on this Mac";

/** One account choice, as the menu and ⌘K offer it. */
export interface RepoAccountChoice {
  id: string;
  /** The menu's line. */
  label: string;
  /** The quieter line under it. */
  description: string;
  /** ⌘K's row, naming the repository. */
  paletteLabel: string;
  checked: boolean;
  intent: RepoAccountIntent;
}

/** `owner/repo`, as GitHub spells it in the PR's URL. */
export const repositoryName = ({ owner, repo }: PullRequestRepository) => `${owner}/${repo}`;

/**
 * Automatic, then every account gh is signed in to on this Mac. A saved login gh no longer
 * lists stays, checked, so the user sees what's chosen and can move off it.
 */
export function repoAccountChoices({
  repository,
  intent,
  accounts,
  automaticLogin,
}: {
  repository: PullRequestRepository;
  intent: RepoAccountIntent;
  accounts: readonly MacAccount[];
  /** Who Automatic reads as now: this Mac's account for the host, once known. */
  automaticLogin: string | null;
}): RepoAccountChoice[] {
  const name = repositoryName(repository);
  const chosen = intent.mode === "account" ? intent.login.toLowerCase() : null;
  const listed = accounts.filter((account) => account.state === "success");
  const missing =
    chosen && !listed.some((account) => account.login.toLowerCase() === chosen)
      ? [{ login: (intent as { login: string }).login, active: false, signedIn: false }]
      : [];
  const id = (part: string) => `repo-account:${repository.host}/${name.toLowerCase()}:${part}`;
  return [
    {
      id: id("automatic"),
      label: automaticLogin ? `Automatic (@${automaticLogin})` : "Automatic",
      description: `Uses this Mac’s account for ${repository.host}.`,
      paletteLabel: `Read ${name} Pull Requests as This Mac’s Account`,
      checked: intent.mode === "automatic",
      intent: { mode: "automatic" },
    },
    ...[
      ...listed.map((account) => ({
        login: account.login,
        active: account.active,
        signedIn: true,
      })),
      ...missing,
    ].map((account) => ({
      id: id(account.login.toLowerCase()),
      label: `@${account.login}`,
      description: !account.signedIn
        ? "Not signed in to gh on this Mac."
        : account.active
          ? "gh’s active account on this Mac."
          : "Signed in to gh on this Mac.",
      paletteLabel: `Read ${name} Pull Requests as @${account.login}`,
      checked: chosen === account.login.toLowerCase(),
      intent: { mode: "account" as const, login: account.login },
    })),
  ];
}

/** The menu's note under the group: the choice is kept for the repository, on this device. */
export function repoAccountNote(repository: PullRequestRepository): string {
  return `Remembered for ${repositoryName(repository)} on this Mac.`;
}

/**
 * The status line for a repository whose login can't be used: why, in plain words, or null
 * where it can. Nothing is read or changed as another account meanwhile.
 */
export function repoAccountProblem(state: RepoReaderState, signedOut: boolean): string | null {
  if (state.kind === "named") {
    return signedOut
      ? `@${state.reader.login} isn’t signed in to gh on this Mac. Run gh auth login, or choose another account.`
      : null;
  }
  if (state.kind !== "blocked") return null;
  const login = state.reader ? `@${state.reader.login}` : null;
  switch (state.reason) {
    case "pending":
    case "checking":
      return login
        ? `Checking whether this Mac can read as ${login}…`
        : "Checking which account reads this repository on this Mac…";
    case "turned_off":
      return `Using other GitHub accounts on this Mac is turned off, so nothing is read as ${login}.`;
    case "update_needed":
      return `Update the app to read this repository as ${login}. Nothing is read as another account meanwhile.`;
  }
}
