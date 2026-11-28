// Items for the "Link a pull request" picker: PRs the client already knows
// about, ones GitHub suggests for the account, and the typed link or number
// as the first, selectable item.

import type { QuickPickInputError, QuickPickItem } from "@/components/ui/quick-pick";
import type { Conversation } from "@/hooks/useConversations";
import type {
  PullRequest,
  PullRequestAssociation,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  canonicalPullRequestUrl,
  parsePullRequestReference,
  pullRequestKey,
  type PullRequestReferenceContext,
  type WorkspaceRemoteEvidence,
} from "@/lib/pullRequestReference";

import { type CodeHostContext, repositoryRemoteHosts } from "@/lib/codeHostContext";
import { SESSION_PULL_REQUEST_LIMIT_REASON } from "@/lib/sessionPullRequestMembership";
import type { PullRequestSuggestion, PullRequestSuggestions } from "@/lib/pullRequestSuggestions";

import { conversationDisplayLabel } from "./sidebarNav";

export const CURRENT_BRANCH_GROUP = "Current branch";
export const REPOSITORY_GROUP = "Open in this repository";
export const RECENT_GROUP = "Recently updated";
export const OTHER_SESSIONS_GROUP = "From other sessions";
const OTHER_SESSION_LIMIT = 20;

export interface AttachPullRequestContext extends PullRequestReferenceContext {
  sessionId: string;
  /** Lowercase noun for messages, e.g. "pull request" or "merge request". */
  requestName: string;
  /** Links already associated with the session. */
  linkedUrls: readonly string[];
  /** The session links as many PRs as it can: nothing new can be linked, only named. */
  full?: boolean;
  /** The checked-out branch's PR, when the host found one. */
  branchPullRequest?: Pick<PullRequest, "url" | "number" | "title" | "state" | "is_draft"> | null;
  /** Loaded sessions, most recent first; their PRs are suggestions, never used to resolve numbers. */
  conversations?: readonly Conversation[];
  /** PRs GitHub suggests for the session host's account, once loaded. */
  suggestions?: PullRequestSuggestions | null;
  /** The typed PR as this Mac looked it up, so the item can show what it links. */
  preview?: PullRequestSuggestion | null;
}

/** "Your open pull requests", in the provider's words. */
export function ownOpenGroup(requestName: string): string {
  return `Your open ${requestName}s`;
}

function stateLabel(state: string | null | undefined, isDraft: boolean | undefined) {
  if (isDraft && state?.toUpperCase() === "OPEN") return "Draft";
  switch (state?.toUpperCase()) {
    case "MERGED":
      return "Merged";
    case "CLOSED":
      return "Closed";
    default:
      return undefined;
  }
}

function linkedSet(context: AttachPullRequestContext): Set<string> {
  return new Set(context.linkedUrls.map(pullRequestKey));
}

function repositoryOf(url: string): string | null {
  try {
    const [owner, repo] = new URL(url).pathname.split("/").filter(Boolean);
    return owner && repo ? `${owner}/${repo}` : null;
  } catch {
    return null;
  }
}

function hostOf(url: string): string | null {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return null;
  }
}

/** A compact name for a PR link, such as "owner/repo#123", or the link itself. */
export function pullRequestShortLabel(url: string, numberPrefix: string): string {
  const repository = repositoryOf(url);
  const number = /\/(\d+)\/?$/.exec(url)?.[1];
  return repository && number ? `${repository}${numberPrefix}${number}` : url;
}

/** Suggested PRs, grouped, excluding any already linked to the session. */
export function attachPullRequestSuggestions(
  context: AttachPullRequestContext,
): QuickPickItem<string>[] {
  // A full session has no room for any of them.
  if (context.full) return [];
  const linked = linkedSet(context);
  const seen = new Set<string>();
  const items: QuickPickItem<string>[] = [];
  const add = (
    pr: Pick<PullRequestAssociation, "url" | "number" | "title" | "state" | "is_draft"> & {
      repository?: string | null;
    },
    group: string,
    origin?: string,
  ) => {
    const id = canonicalPullRequestUrl(pr.url);
    if (linked.has(pullRequestKey(id)) || seen.has(id)) return;
    seen.add(id);
    const repository = pr.repository ?? repositoryOf(pr.url);
    const reference = `${repository ?? ""}${context.numberPrefix}${pr.number}`;
    items.push({
      id,
      label: pr.title?.trim() || reference,
      description: origin ? `${reference} · ${origin}` : reference,
      detail: stateLabel(pr.state, pr.is_draft),
      group,
      keywords: [String(pr.number), pr.url],
      value: pr.url,
    });
  };

  if (context.branchPullRequest) {
    add({ ...context.branchPullRequest, repository: context.repository }, CURRENT_BRANCH_GROUP);
  }
  const suggestions = context.suggestions;
  for (const pr of suggestions?.repository ?? []) add(pr, REPOSITORY_GROUP);
  for (const pr of suggestions?.mine ?? []) add(pr, ownOpenGroup(context.requestName));
  for (const pr of suggestions?.recent ?? []) add(pr, RECENT_GROUP);
  // The same owner/repo on another host is another repository.
  const repositoryHosts = new Set(
    context.sessionPullRequests
      .filter((pr) => pr.repository.toLowerCase() === context.repository?.toLowerCase())
      .map((pr) => pr.host.toLowerCase()),
  );
  let fromOtherSessions = 0;
  for (const conversation of context.conversations ?? []) {
    if (fromOtherSessions >= OTHER_SESSION_LIMIT) break;
    const pr = conversation.pull_request;
    if (!pr || conversation.id === context.sessionId || conversation.archived) continue;
    if (context.provider && pr.provider && pr.provider !== context.provider) continue;
    if (context.repository && pr.repository.toLowerCase() !== context.repository.toLowerCase()) {
      continue;
    }
    const host = hostOf(pr.url);
    if (repositoryHosts.size > 0 && host && !repositoryHosts.has(host)) continue;
    const before = items.length;
    add(pr, OTHER_SESSIONS_GROUP, conversationDisplayLabel(conversation));
    if (items.length > before) fromOtherSessions += 1;
  }
  return items;
}

/**
 * The empty state's hint. Where a number resolves, the example is this
 * repository's own link; otherwise it's `providerExample`, or none for
 * providers whose links vary.
 */
export function attachPullRequestHint(
  context: AttachPullRequestContext,
  providerExample: string | null,
): string {
  const typed = `${context.numberPrefix}123`;
  const resolved = parsePullRequestReference(typed, context);
  const example = resolved?.kind === "link" ? resolved.url : providerExample;
  return example
    ? `Paste a link like ${example}, or type ${typed}.`
    : `Paste a link, or type ${typed}.`;
}

/**
 * The workspace remote's evidence from its read: `reading` while a read is in
 * flight, even over a cached answer, or while the helper is still starting;
 * otherwise what the answer says about `repository`. Undefined where the
 * remotes can't be read, which leaves the other sources.
 */
export function workspaceRemoteEvidence(
  read: { data: CodeHostContext | undefined; isFetching: boolean },
  repository: string | null,
): WorkspaceRemoteEvidence | undefined {
  // A read in flight may name a different host than a cached answer, so wait for it.
  if (read.isFetching || read.data?.status === "pending") return { status: "reading" };
  if (read.data === undefined) return undefined;
  const remote = repositoryRemoteHosts(read.data, repository);
  return remote ? { status: "read", ...remote } : undefined;
}

/** Whether the typed number waits on the workspace's remotes before it can link. */
export function attachPullRequestWaitsForRemote(
  input: string,
  context: AttachPullRequestContext,
): boolean {
  return parsePullRequestReference(input, context)?.kind === "pending";
}

/** The typed link or number as an item, an inline error, or null for search text. */
export function attachPullRequestFromInput(
  input: string,
  context: AttachPullRequestContext,
): QuickPickItem<string> | QuickPickInputError | null {
  const reference = parsePullRequestReference(input, context);
  // A number still waiting on the workspace's remotes offers nothing to link yet.
  if (!reference || reference.kind === "pending") return null;
  if (reference.kind === "invalid") return { error: reference.error };
  const id = canonicalPullRequestUrl(reference.url);
  if (linkedSet(context).has(pullRequestKey(id))) {
    return { error: `That ${context.requestName} is already linked to this session.` };
  }
  if (context.full) return { error: SESSION_PULL_REQUEST_LIMIT_REASON };
  const label =
    reference.repository && reference.number !== null
      ? `Link ${reference.repository}${context.numberPrefix}${reference.number}`
      : `Link ${reference.url}`;
  const preview = context.preview;
  if (preview && canonicalPullRequestUrl(preview.url) === id) {
    const shortName = `${preview.repository ?? repositoryOf(preview.url) ?? ""}${context.numberPrefix}${preview.number}`;
    return {
      id,
      label: preview.title.trim() ? `Link “${preview.title.trim()}”` : label,
      description: shortName,
      detail: stateLabel(preview.state, preview.is_draft),
      rememberInHistory: false,
      value: reference.url,
    };
  }
  return {
    id,
    label,
    description: reference.number !== null ? reference.url : undefined,
    rememberInHistory: false,
    value: reference.url,
  };
}
