// Provider-visible copy for the pull request panel, keyed by the info payload's
// `provider` id. A host that predates the field serves GitHub.

import type { ComponentType } from "react";
import AzureMono from "@lobehub/icons/es/Azure/components/Mono";
import GithubMono from "@lobehub/icons/es/Github/components/Mono";
import { GitPullRequestIcon } from "lucide-react";
import { GitLabIcon } from "@/components/icons/GitLabIcon";

export interface GitProviderDisplay {
  id: string;
  display_name: string;
  request_name: string;
  number_prefix: string;
}

/** A provider glyph; lobehub brand icons and lucide icons both fit. */
export type GitProviderIcon = ComponentType<{ size?: number | string; className?: string }>;

/** What the panel shows for one git provider. In hints, `backticked` text renders as code. */
export interface GitProviderCopy {
  /** The info payload's `provider` id. */
  id: string;
  /** Display name, e.g. "GitHub". */
  label: string;
  requestName: string;
  Icon: GitProviderIcon;
  /** Shown before a PR number, e.g. "#" in "#123". */
  prNumberPrefix: string;
  /** Example URL in the link-a-PR input. */
  prUrlPlaceholder: string;
  /** Host left out of PR labels; null shows every host. */
  defaultHost: string | null;
  /** How to sign in on the host. */
  authHint: string;
  /** Name of the provider's CLI, as in "Install the GitHub CLI". */
  cliLabel: string;
  /** The provider can sign in without its CLI (a token), so a missing CLI also shows `authHint`. */
  signInWithoutCli?: boolean;
  /** Hint when the upstream repo can't be reached; `authHint` when absent. */
  repoUnresolvedHint?: string;
}

const GITHUB: GitProviderCopy = {
  id: "github",
  label: "GitHub",
  requestName: "pull request",
  Icon: GithubMono,
  prNumberPrefix: "#",
  prUrlPlaceholder: "https://github.com/owner/repo/pull/123",
  defaultHost: "github.com",
  authHint: "Run gh auth login on the host.",
  cliLabel: "GitHub CLI",
  repoUnresolvedHint:
    "Pick the account to use, or run `gh auth status` on the host to confirm the GitHub CLI is signed in.",
};

/** Shown while no provider is known: no payload yet, or none serves the workspace. */
const NEUTRAL: GitProviderCopy = {
  id: "",
  label: "Pull Requests",
  requestName: "pull request",
  Icon: GitPullRequestIcon,
  prNumberPrefix: "#",
  prUrlPlaceholder: "Pull request URL",
  defaultHost: null,
  authHint: "Sign in to your git provider on the host.",
  cliLabel: "Git provider CLI",
};

/** Copy for each known provider id. */
export const GIT_PROVIDERS: Readonly<Record<string, GitProviderCopy>> = {
  github: GITHUB,
};

/**
 * The copy for a provider id. No id (a host that predates `provider`) is GitHub;
 * `null` (no provider is known) is neutral copy that names none; an unknown id
 * gets generic copy that uses the host's `auth.hint` when given.
 */
export function gitProviderCopy(
  id?: string | null,
  authHint?: string | null,
  display?: GitProviderDisplay | null,
): GitProviderCopy {
  if (id === null) return NEUTRAL;
  if (!id) return GITHUB;
  if (Object.hasOwn(GIT_PROVIDERS, id)) return GIT_PROVIDERS[id];
  const metadata = display?.id === id ? display : undefined;
  const label = metadata?.display_name || id;
  const requestName = metadata?.request_name || "pull request";
  return {
    id,
    label,
    requestName,
    Icon: id === "gitlab" ? GitLabIcon : id === "azure_devops" ? AzureMono : GitPullRequestIcon,
    prNumberPrefix: metadata?.number_prefix ?? "#",
    prUrlPlaceholder: `${requestName[0].toUpperCase()}${requestName.slice(1)} URL`,
    defaultHost: null,
    authHint: authHint || `Sign in to ${label} on the host.`,
    signInWithoutCli: authHint ? true : undefined,
    cliLabel: `${label} CLI`,
  };
}
