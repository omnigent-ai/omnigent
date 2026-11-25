// GitSpoke pull requests linked to a session on this Mac only. The server
// stores only links its host can verify, so the desktop app keeps these itself,
// for the signed-in workspace user. They don't sync to other devices.

import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  isDesktopCodeHostAvailable,
  parseCodeHostPullRequestUrl,
  queryCodeHost,
} from "@/lib/codeHosts";
import { PANEL_READ_OPERATIONS } from "@/lib/desktopReadPath";
import { getCurrentUserId } from "@/lib/identity";
import {
  desktopLocalPullRequestLinks,
  getDesktopFeatures,
  hasLocalPullRequestLinksBridge,
  type LocalPullRequestLink,
} from "@/lib/nativeBridge";

/** What reading a GitSpoke PR on this Mac needs: the panel's reads, any provider's. */
export const GITSPOKE_READ_OPERATIONS = PANEL_READ_OPERATIONS;

const GITSPOKE_DISPLAY = {
  id: "gitspoke",
  display_name: "GitSpoke",
  request_name: "pull request",
  number_prefix: "#",
};

/** Whether this app can link `url` on this Mac: a GitSpoke PR, and a shell that reads it. */
export async function canLinkOnThisMac(url: string): Promise<boolean> {
  if (parseCodeHostPullRequestUrl(url)?.provider !== "gitspoke") return false;
  return (await localLinkScope()) !== null;
}

/**
 * Where this shell keeps links for the signed-in user. Listing and removing need
 * only this, so links stay visible and removable after GitSpoke reads turn off.
 */
async function localLinkStorageScope(): Promise<{ userId: string } | null> {
  // Browsers and older shells have no store; nothing else needs asking.
  if (!hasLocalPullRequestLinksBridge()) return null;
  const userId = getCurrentUserId();
  if (!userId) return null;
  const features = await getDesktopFeatures();
  return features?.localPullRequestLinks === true ? { userId } : null;
}

/** The storage scope, when this Mac can also read GitSpoke PRs, which linking needs. */
async function localLinkScope(): Promise<{ userId: string } | null> {
  const scope = await localLinkStorageScope();
  if (!scope) return null;
  return (await isDesktopCodeHostAvailable("gitspoke", GITSPOKE_READ_OPERATIONS)) ? scope : null;
}

function association(link: LocalPullRequestLink): PullRequestAssociation {
  return {
    url: link.url,
    host: link.host,
    repository: link.repository,
    number: link.number,
    relationship: "attached",
    provider: "gitspoke",
    provider_display: GITSPOKE_DISPLAY,
    saved_on_device: true,
  };
}

/**
 * The query key for a session's links kept on this Mac, under the workspace user
 * they were saved for; refreshed after each change.
 */
export const localPullRequestLinksKey = (sessionId: string | undefined) =>
  ["local-pull-request-links", getCurrentUserId(), sessionId] as const;

/** The session's links kept on this Mac, as associations; none where the shell can't keep them. */
export async function listLocalPullRequests(sessionId: string): Promise<PullRequestAssociation[]> {
  const scope = await localLinkStorageScope();
  if (!scope) return [];
  const links = await desktopLocalPullRequestLinks({ action: "list", sessionId, ...scope });
  return (links ?? []).map(association);
}

/** The query key for every session's links kept on this Mac, under the workspace user. */
export const localPullRequestLinksByUserKey = () =>
  ["local-pull-request-links-by-user", getCurrentUserId()] as const;

/**
 * Every session's links kept on this Mac, by session id, in one request; empty on
 * shells that can't list them all at once.
 */
export async function listLocalPullRequestsByUser(): Promise<
  Map<string, PullRequestAssociation[]>
> {
  const bySession = new Map<string, PullRequestAssociation[]>();
  const scope = await localLinkStorageScope();
  if (!scope || (await getDesktopFeatures())?.localPullRequestLinksByUser !== true) {
    return bySession;
  }
  const links = await desktopLocalPullRequestLinks({ action: "list_user", ...scope });
  for (const link of links ?? []) {
    if (typeof link.session_id !== "string" || !link.session_id) continue;
    const session = bySession.get(link.session_id) ?? [];
    session.push(association(link));
    bySession.set(link.session_id, session);
  }
  return bySession;
}

/** Keep `url` linked to the session on this Mac; throws when the shell can't. */
export async function addLocalPullRequest(
  sessionId: string,
  url: string,
): Promise<PullRequestAssociation[]> {
  const scope = await localLinkScope();
  const links = scope
    ? await desktopLocalPullRequestLinks({ action: "add", sessionId, url, ...scope })
    : null;
  if (!links) throw new Error("This Mac couldn't save that link. Try again.");
  return links.map(association);
}

/** Unlink `url` from the session on this Mac; throws when the shell can't. */
export async function removeLocalPullRequest(
  sessionId: string,
  url: string,
): Promise<PullRequestAssociation[]> {
  const scope = await localLinkStorageScope();
  const links = scope
    ? await desktopLocalPullRequestLinks({ action: "remove", sessionId, url, ...scope })
    : null;
  if (!links) throw new Error("This Mac couldn't remove that link. Try again.");
  return links.map(association);
}

/** Forget the session's links on this Mac, once the session is deleted. */
export async function forgetLocalPullRequestSession(sessionId: string): Promise<void> {
  const scope = await localLinkStorageScope();
  if (scope) await desktopLocalPullRequestLinks({ action: "forget_session", sessionId, ...scope });
}

/**
 * Forget every link kept on this Mac for this server, before its account signs
 * out. False when the shell couldn't, so sign-out can say what's left behind.
 */
export async function forgetLocalPullRequestServer(): Promise<boolean> {
  if (!hasLocalPullRequestLinksBridge()) return true;
  return (await desktopLocalPullRequestLinks({ action: "forget_server" })) !== null;
}

/** Whether this app keeps GitSpoke links on this Mac for the signed-in user. */
export async function canKeepLinksOnThisMac(): Promise<boolean> {
  return (await localLinkScope()) !== null;
}

/**
 * Link a GitSpoke PR on this Mac after confirming this Mac can read it. Null when
 * `url` isn't one this Mac links, so the caller links it through the host.
 */
export async function linkPullRequestOnThisMac(
  sessionId: string,
  url: string,
): Promise<{ url: string; links: PullRequestAssociation[] } | null> {
  const target = parseCodeHostPullRequestUrl(url);
  if (target?.provider !== "gitspoke" || !(await localLinkScope())) return null;
  const confirmed = await queryCodeHost({
    provider: "gitspoke",
    host: target.host,
    operation: "pull_request_lookup",
    params: { repository: target.repository, number: target.number },
  });
  if (!confirmed.ok) throw new Error(confirmed.error.message);
  return { url: target.url, links: await addLocalPullRequest(sessionId, target.url) };
}

/** One PR's identity across sources: its host, repository and number. */
function identityKey(pr: Pick<PullRequestAssociation, "host" | "repository" | "number">): string {
  return `${pr.host.toLowerCase()}/${pr.repository.toLowerCase()}#${pr.number}`;
}

/** `primary` followed by the local links it doesn't already hold. */
export function withLocalPullRequests(
  primary: readonly PullRequestAssociation[],
  local: readonly PullRequestAssociation[],
): PullRequestAssociation[] {
  // Every entry counts here, this Mac's own included, so a list never holds a PR twice.
  const seen = new Set(primary.map(identityKey));
  return [...primary, ...local.filter((pr) => !seen.has(identityKey(pr)))];
}

/** The local links `server` doesn't already hold: the PRs only this Mac links. */
export function localOnlyPullRequests(
  server: readonly Pick<
    PullRequestAssociation,
    "host" | "repository" | "number" | "saved_on_device"
  >[],
  local: readonly PullRequestAssociation[],
): PullRequestAssociation[] {
  // A list that already merged this Mac's links holds them too; those aren't the server's.
  const seen = new Set(server.filter((pr) => pr.saved_on_device !== true).map(identityKey));
  return local.filter((pr) => !seen.has(identityKey(pr)));
}

/** Whether the session's link to `url` is kept on this Mac rather than on the server. */
export function isLocalPullRequest(
  url: string,
  prs: readonly Pick<PullRequestAssociation, "url" | "saved_on_device">[] | undefined,
): boolean {
  return prs?.some((pr) => pr.url === url && pr.saved_on_device === true) ?? false;
}
