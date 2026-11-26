// What a build adds to the Pull Requests page beyond GitHub: code hosts it knows by name, a
// review site its PRs link to, stack description formats, a merge readiness source, and
// reviewer pings. A build that registers nothing shows the page with GitHub alone.

import type { QueryClient } from "@tanstack/react-query";
import type { ComponentType } from "react";

import type { GitProviderCopy } from "@/lib/gitProviders";
import type { MergeReadinessPresentation, MergeReadinessView } from "@/lib/mergeReadiness";
import type { CodeHostProviderId } from "@/lib/codeHosts";
import type { GitHubFastTrackAccount } from "@/lib/nativeBridgeCodeHost";
import type { StackFormat } from "@/lib/pullRequestStacks/stackModel";
import type { CommandAvailability } from "@/store/paletteCommands";

/** A code host recognized by its name alone, without other evidence. */
export interface NamedCodeHost {
  provider: CodeHostProviderId;
  matchesHost: (host: string) => boolean;
  /** How the app names the provider and what it tells the user about it. */
  copy: GitProviderCopy;
  /** Whether GitHub hosts its pull requests, so GitHub's own pages apply to them. */
  hostedOnGitHub: boolean;
  /** This Mac reads its pull requests as the Mac's own identity, never as a `gh` account. */
  readsAsDevice: boolean;
  /** Changes from this app don't apply to its pull requests; they're made on the host itself. */
  readOnly: boolean;
  /** Links to its pull requests are kept on this Mac, since the server can't verify them. */
  linksOnDevice: boolean;
}

/** A site with its own page for reviewing a pull request, which the PR's menus link to. */
export interface PullRequestReviewSite {
  /** How menus name it, e.g. "Open in {name}". */
  name: string;
  /** Its page for a pull request URL, or null where it has none. */
  reviewUrl: (pullRequestUrl: string) => string | null;
  /** Whether its links show here; a hook, read on every render. */
  useEnabled: () => boolean;
}

/** A PR to read merge readiness for, as its own host account or this Mac's. */
export interface MergeReadinessTarget {
  url: string;
  account: GitHubFastTrackAccount | null;
  /** Set for a PR this Mac reads: GitHub is read as the Mac's account, under this scope. */
  mac?: { scope: readonly unknown[] };
  /** The head the session's host reports, when it has one. */
  headSha: string | null | undefined;
}

/** `active` false (a hidden panel) pauses reads and polling. */
export interface MergeReadinessReadOptions {
  active: boolean;
  pollMs: number | false;
}

/**
 * What one mounted readiness reader has rendered of a GitHub readiness read: when that answer
 * settled, and the checks read it then requires (by query hash), or null when it needs none.
 */
export interface MergeReadinessRequirement {
  appliedAt: number;
  checks: string | null;
}

/** A pull request whose changed files the reader may own, read for its head. */
export interface OwnedFilesTarget {
  url: string | null | undefined;
  /** The GitHub login the PR is read as, whose ownership to answer. */
  viewerLogin: string | null | undefined;
  headSha: string | null | undefined;
}

/** A service that says whether a pull request is ready to merge, beyond GitHub's own status. */
export interface MergeReadinessSource {
  /** The GitHub read the source joins with its own, which the Mac must offer to read it there. */
  githubOperation: string;
  /** Each target's view, by URL; a hook. */
  useMergeReadiness: (
    targets: readonly MergeReadinessTarget[],
    options: MergeReadinessReadOptions,
  ) => ReadonlyMap<string, MergeReadinessView>;
  /** Reads every PR's readiness again. */
  refresh: (queryClient: QueryClient) => Promise<void>;
  /** Reads one PR's readiness again. */
  refreshPullRequest: (queryClient: QueryClient, prUrl: string) => Promise<void>;
  /**
   * Which of `prUrl`'s readiness reads a query is: the readiness itself, whose readers publish
   * the checks it needs, or one of those checks; null for any other query.
   */
  readOf: (queryKey: readonly unknown[], prUrl: string) => "readiness" | "checks" | null;
  /** The label, tint and reason for a view other than `off`. */
  present: (view: Exclude<MergeReadinessView, { kind: "off" }>) => MergeReadinessPresentation;
  /** Shown under the status when the source needs the user to renew a credential. */
  CredentialRenewal: ComponentType<{ onRenewed: () => void }>;
  /** The paths the reader may approve in the target's PR, or null while unknown; a hook. */
  useOwnedFiles: (
    target: OwnedFilesTarget,
    options: MergeReadinessReadOptions,
  ) => readonly string[] | null;
}

/** Where a reviewer ping is offered. */
export type ReviewerPingSurface = "reviewer_row" | "review_menu" | "palette";

/** The pull request a reviewer could be pinged about. */
export interface ReviewerPingPullRequest {
  url: string;
  number: number;
  /** GitHub's state, e.g. "OPEN"; anything else can't be pinged about. */
  state: string | null | undefined;
  isDraft?: boolean;
  author: string | null | undefined;
  reviewers: readonly { login: string; kind: "user" | "team"; state: string }[];
}

/** One reviewer's ping, as a command. */
export interface ReviewerPingAction {
  command: {
    id: string;
    /** The label in a reviewer's own row or a menu that lists reviewers. */
    menuLabel: string;
    /** The label in ⌘K, which names the reviewer and the PR. */
    paletteLabel: string;
    keywords: readonly string[];
    availability: CommandAvailability;
  };
  run: () => void;
}

/** A way to nudge a requested reviewer outside the code host. */
export interface ReviewerPings {
  /** The heading over a menu's pings, e.g. "Ping reviewers on Slack". */
  sectionLabel: string;
  /** The ping for each pingable reviewer, keyed by login in lower case; a hook. */
  useActions: (
    pr: ReviewerPingPullRequest | null,
    surface: ReviewerPingSurface,
  ) => ReadonlyMap<string, ReviewerPingAction>;
  /** Adds ⌘K's ping rows for the pull request in view, while it shows. */
  PaletteCommands: ComponentType<{ pr: ReviewerPingPullRequest | null; active: boolean }>;
}

export interface PullRequestIntegrations {
  namedCodeHosts: readonly NamedCodeHost[];
  /** Stack description formats, in the order they're tried. */
  stackFormats: readonly StackFormat[];
  reviewSite: PullRequestReviewSite | null;
  mergeReadiness: MergeReadinessSource | null;
  reviewerPings: ReviewerPings | null;
}

const NONE: PullRequestIntegrations = {
  namedCodeHosts: [],
  stackFormats: [],
  reviewSite: null,
  mergeReadiness: null,
  reviewerPings: null,
};

let current: PullRequestIntegrations = NONE;

/** `list` with `added` merged in: an entry with a key already there takes its place. */
function mergeByKey<Entry>(
  list: readonly Entry[],
  added: readonly Entry[] | undefined,
  key: (entry: Entry) => string,
): readonly Entry[] {
  const merged = [...list];
  for (const entry of added ?? []) {
    const at = merged.findIndex((existing) => key(existing) === key(entry));
    if (at === -1) merged.push(entry);
    else merged[at] = entry;
  }
  return merged;
}

/**
 * Adds a build's integrations, once at startup before the page renders: hooks they carry are
 * called on every render, so they must not change while it shows.
 */
export function registerPullRequestIntegrations(added: Partial<PullRequestIntegrations>): void {
  current = {
    // Registering again, as a hot reload does, replaces entries rather than repeating them.
    namedCodeHosts: mergeByKey(
      current.namedCodeHosts,
      added.namedCodeHosts,
      (host) => host.provider,
    ),
    stackFormats: mergeByKey(current.stackFormats, added.stackFormats, (format) => format.id),
    reviewSite: added.reviewSite ?? current.reviewSite,
    mergeReadiness: added.mergeReadiness ?? current.mergeReadiness,
    reviewerPings: added.reviewerPings ?? current.reviewerPings,
  };
}

export function pullRequestIntegrations(): PullRequestIntegrations {
  return current;
}

/** Puts back the integrations from before a test registered its own. */
export function restorePullRequestIntegrations(saved: PullRequestIntegrations): void {
  current = saved;
}
