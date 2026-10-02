// Provider-visible copy for the pull request panel and the new-chat repository
// picker, keyed by the provider id. A host that predates the field serves GitHub.

import type { ComponentType } from "react";
import AzureMono from "@lobehub/icons/es/Azure/components/Mono";
import GithubMono from "@lobehub/icons/es/Github/components/Mono";
import { GitPullRequestIcon } from "lucide-react";

/** A provider glyph; lobehub brand icons and lucide icons both fit. */
export type GitProviderIcon = ComponentType<{ size?: number | string; className?: string }>;

/** What the panel shows for one git provider. In hints, `backticked` text renders as code. */
export interface GitProviderCopy {
  /** The info payload's `provider` id. */
  id: string;
  /** Display name, e.g. "GitHub". */
  label: string;
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
  /** Clone URL of a repo the connection lists; absent when the API must supply `clone_url`. */
  cloneUrlFor?: (fullName: string) => string;
}

const GITHUB: GitProviderCopy = {
  id: "github",
  label: "GitHub",
  Icon: GithubMono,
  prNumberPrefix: "#",
  prUrlPlaceholder: "https://github.com/owner/repo/pull/123",
  defaultHost: "github.com",
  authHint: "Run gh auth login on the host.",
  cliLabel: "GitHub CLI",
  repoUnresolvedHint:
    "Pick the account to use, or run `gh auth status` on the host to confirm the GitHub CLI is signed in.",
  cloneUrlFor: (fullName) => `https://github.com/${fullName}.git`,
};

const AZURE_DEVOPS: GitProviderCopy = {
  id: "azure_devops",
  label: "Azure DevOps",
  // The icon package has no Azure DevOps mark; the Azure mark stands in.
  Icon: AzureMono,
  // Azure DevOps references pull requests as !123.
  prNumberPrefix: "!",
  prUrlPlaceholder: "https://dev.azure.com/org/project/_git/repo/pullrequest/123",
  defaultHost: "dev.azure.com",
  authHint: "Run `az login` on the host, or set `AZURE_DEVOPS_EXT_PAT`.",
  cliLabel: "Azure CLI",
  signInWithoutCli: true,
};

/** Shown while no provider is known: no payload yet, or none serves the workspace. */
const NEUTRAL: GitProviderCopy = {
  id: "",
  label: "Pull Requests",
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
  azure_devops: AZURE_DEVOPS,
};

/**
 * The copy for a provider id. No id (a host that predates `provider`) is GitHub;
 * `null` (no provider is known) is neutral copy that names none; an unknown id
 * gets generic copy that uses the host's `auth.hint` when given.
 */
export function gitProviderCopy(id?: string | null, authHint?: string | null): GitProviderCopy {
  if (id === null) return NEUTRAL;
  if (!id) return GITHUB;
  if (Object.hasOwn(GIT_PROVIDERS, id)) return GIT_PROVIDERS[id];
  return {
    id,
    label: id,
    Icon: GitPullRequestIcon,
    prNumberPrefix: "#",
    prUrlPlaceholder: "Pull request URL",
    defaultHost: null,
    authHint: authHint || `Sign in to ${id} on the host.`,
    cliLabel: `${id} CLI`,
  };
}
