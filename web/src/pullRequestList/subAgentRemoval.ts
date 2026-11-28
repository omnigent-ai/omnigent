// Removing a row from a session's Pull Requests list, sub-agents' rows included: it unlinks the
// PR from each session in the shown tree that links it and that the user may change, and names
// the others instead of touching them. Every removal route, label and outcome derives from here.

import { canonicalPullRequestUrl } from "@/lib/pullRequestReference";

import type { PullRequestSessionSource, SourceSession } from "./subAgentPullRequests";

/** The single-user server's viewer, who owns every session. */
const SINGLE_USER_VIEWER = "local";

/** One link to remove: `url` as `sessionId` links it. */
export interface RemovalTarget {
  sessionId: string;
  url: string;
}

/** What removing one row does: the links it removes, and the sessions it leaves linking it. */
export interface RowRemoval {
  url: string;
  targets: RemovalTarget[];
  /** The sub-agents the row's links come from that removal unlinks it from, by name. */
  fromSubAgents: SourceSession[];
  /** Sessions that link the PR but the user doesn't own, so it stays linked there. */
  kept: SourceSession[];
  /** Whether `kept` stays only because who owns the tree hasn't been read yet. */
  ownershipPending: boolean;
}

/**
 * Whether the user owns the shown session tree, so its sub-agents' links are theirs to remove:
 * the root's owner grant names them. Without a permission store the server names no owner, and
 * then only its single user owns it. `undefined` while either read is out.
 *
 * @param rootOwner - The root session's owner, `null` when the server names none.
 * @param viewerId - The signed-in user, e.g. `"local"` on a single-user server.
 */
export function ownsSessionTree(
  rootOwner: string | null | undefined,
  viewerId: string | null,
): boolean | undefined {
  if (rootOwner === undefined || viewerId === null) return undefined;
  if (rootOwner === null) return viewerId === SINGLE_USER_VIEWER;
  return rootOwner === viewerId;
}

/**
 * The removal of `url`'s row, from the sources the merged list gave it. The viewed session's
 * own link needs only edit access to the list; a sub-agent's needs the owned tree.
 *
 * @param source - The row's sources; absent for a row only the viewed session links.
 * @param ownsTree - Whether the user owns the shown tree; not yet known counts as no.
 */
export function rowRemoval({
  url,
  source,
  viewedSessionId,
  canEditViewed,
  ownsTree,
}: {
  url: string;
  source: PullRequestSessionSource | undefined;
  viewedSessionId: string;
  canEditViewed: boolean;
  ownsTree: boolean | undefined;
}): RowRemoval {
  const targets: RemovalTarget[] = [];
  const fromSubAgents: SourceSession[] = [];
  const kept: SourceSession[] = [];
  const seen = new Set<string>();
  const ownedSubAgents = ownsTree === true;
  if (!source || source.session === null) {
    if (canEditViewed) {
      seen.add(viewedSessionId);
      targets.push({ sessionId: viewedSessionId, url });
    }
  }
  for (const session of source ? [source.session, ...source.alsoIn] : []) {
    if (!session || seen.has(session.id)) continue;
    seen.add(session.id);
    if (ownedSubAgents) {
      targets.push({ sessionId: session.id, url });
      fromSubAgents.push(session);
    } else {
      kept.push(session);
    }
  }
  return {
    url,
    targets,
    fromSubAgents,
    kept,
    ownershipPending: ownsTree === undefined && kept.length > 0,
  };
}

/**
 * The removal of each row a list shows, by any spelling of its URL. A URL the list doesn't
 * show, such as one a stale selection or command still holds, removes nothing.
 *
 * @param shown - The URLs of the rows the list shows, as it shows them.
 */
export function listRemovalOf({
  shown,
  sources,
  viewedSessionId,
  canEditViewed,
  ownsTree,
}: {
  shown: readonly string[];
  sources: ReadonlyMap<string, PullRequestSessionSource> | undefined;
  viewedSessionId: string;
  canEditViewed: boolean;
  ownsTree: boolean | undefined;
}): (url: string) => RowRemoval | undefined {
  const shownByKey = new Map(shown.map((url) => [canonicalPullRequestUrl(url), url]));
  return (url) => {
    const shownUrl = shownByKey.get(canonicalPullRequestUrl(url));
    if (shownUrl === undefined) return undefined;
    return rowRemoval({
      url: shownUrl,
      source: sources?.get(shownUrl),
      viewedSessionId,
      canEditViewed,
      ownsTree,
    });
  };
}

/** The removals of the `urls` the list shows, once each; a URL it doesn't show removes nothing. */
export function rowRemovals(
  urls: readonly string[],
  removalOf: (url: string) => RowRemoval | undefined,
): RowRemoval[] {
  const byKey = new Map<string, RowRemoval>();
  for (const url of urls) {
    const removal = removalOf(url);
    const key = canonicalPullRequestUrl(url);
    if (removal && removal.targets.length > 0 && !byKey.has(key)) byKey.set(key, removal);
  }
  return [...byKey.values()];
}

/**
 * How a list's Remove of `urls` runs: nothing where none is shown and removable, a run across
 * the tree where any reaches past this session's own links, or else an unlink of just those.
 * Never the requested URLs themselves, which may be stale.
 */
export function unlinkPlan(
  urls: readonly string[],
  removalOf: (url: string) => RowRemoval | undefined,
): { kind: "none" } | { kind: "tree"; removals: RowRemoval[] } | { kind: "own"; urls: string[] } {
  const removals = rowRemovals(urls, removalOf);
  if (removals.length === 0) return { kind: "none" };
  if (removals.some((removal) => removal.fromSubAgents.length > 0 || removal.kept.length > 0)) {
    return { kind: "tree", removals };
  }
  return { kind: "own", urls: removals.map((removal) => removal.url) };
}

/** Every session's name, as a sentence lists them: "a", "a and b", "a, b and c". */
const names = (sessions: readonly SourceSession[]) => {
  const all = sessions.map((session) => session.name);
  return all.length <= 2 ? all.join(" and ") : `${all.slice(0, -1).join(", ")} and ${all.at(-1)}`;
};

const THIS_SESSION: SourceSession = { id: "", name: "this session" };

const keptNoteFor = (kept: readonly SourceSession[], ownershipPending: boolean) =>
  kept.length === 0
    ? null
    : ownershipPending
      ? `Checking whether you own ${names(kept)}…`
      : `Still linked by ${names(kept)}, which you don’t own.`;

/** How a row's Remove reads in its menu and ⌘K, and why it can't, where it can't. */
export function removalDescription(removal: RowRemoval): {
  menuLabel: string;
  detail: string | null;
  unavailableReason: string | null;
} {
  const own = removal.targets.length > removal.fromSubAgents.length;
  const agents = removal.fromSubAgents;
  const keptNote = keptNoteFor(removal.kept, removal.ownershipPending);
  if (removal.targets.length === 0) {
    return {
      menuLabel: "Remove from this session",
      detail: null,
      unavailableReason:
        keptNote ?? "You need edit access to remove pull requests from this session.",
    };
  }
  const menuLabel =
    agents.length === 0
      ? "Remove from this session"
      : !own && agents.length === 1
        ? `Remove from ${agents[0]!.name}`
        : `Remove from ${removal.targets.length} agents`;
  const detail = [
    agents.length > 0 && (own || agents.length > 1)
      ? `Unlinks it from ${names([...(own ? [THIS_SESSION] : []), ...agents])}.`
      : null,
    keptNote,
  ]
    .filter(Boolean)
    .join(" ");
  return { menuLabel, detail: detail || null, unavailableReason: null };
}

/** How one PR's removal came out: all of its links, some, or none. */
export type RemovalOutcome = "removed" | "partial" | "not_removed";

/**
 * A PR's outcome from its links' results. It's removed only when every link went and no
 * session the user doesn't own still links it.
 */
export function removalOutcome(
  removal: RowRemoval,
  removedSessionIds: ReadonlySet<string>,
): RemovalOutcome {
  const removed = removal.targets.filter((target) => removedSessionIds.has(target.sessionId));
  if (removed.length === 0) return "not_removed";
  return removed.length === removal.targets.length && removal.kept.length === 0
    ? "removed"
    : "partial";
}

/**
 * Where removing several rows unlinks them from: in a sentence and in title case for labels,
 * and a detail naming every session they leave and every one that still links them.
 */
export function removalPlace(removals: readonly RowRemoval[]): {
  text: string;
  title: string;
  detail: string | null;
} {
  const own = removals.some((removal) => removal.targets.length > removal.fromSubAgents.length);
  const agents = new Map(
    removals.flatMap((removal) => removal.fromSubAgents.map((session) => [session.id, session])),
  );
  const kept = new Map(
    removals.flatMap((removal) => removal.kept.map((session) => [session.id, session])),
  );
  const keptNote = keptNoteFor(
    [...kept.values()],
    removals.some((removal) => removal.ownershipPending),
  );
  if (agents.size === 0) return { text: "this session", title: "This Session", detail: keptNote };
  const detail = [
    `Unlinks them from ${names([...(own ? [THIS_SESSION] : []), ...agents.values()])}.`,
    keptNote,
  ]
    .filter(Boolean)
    .join(" ");
  if (!own && agents.size === 1) {
    const name = [...agents.values()][0]!.name;
    return { text: name, title: name, detail };
  }
  return own
    ? { text: "this session and its agents", title: "This Session and Its Agents", detail }
    : { text: "this session’s agents", title: "This Session’s Agents", detail };
}
