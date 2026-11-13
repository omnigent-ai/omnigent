// Reconcile only lists whose membership or pagination may have changed.

import {
  type ConversationListFilters,
  type MembershipChange,
  type MembershipField,
  PROJECT_LABEL_KEY,
  SIDE_CHAT_LABEL_KEY,
} from "@/lib/sessionListCache";
import { sessionVisibility } from "@/lib/sessionVisibility";
import type { Conversation } from "@/hooks/useConversations";

/** One cached list, as the target picker sees it after the frame was patched in. */
export interface CachedList {
  /** TanStack Query hash of the list's key. */
  queryHash: string;
  filters: ConversationListFilters;
  /** Project name for a project folder's own list (`["project-sessions", name, …]`). */
  folder?: string;
  /** Ids the list's pages hold after the patch. */
  ids: ReadonlySet<string>;
  /** The patch already decided this list needs a refetch (see `mergeItemsIntoPages`). */
  needsRefetch: boolean;
}

/**
 * Names of the projects a row is filed under, or `null` when its first-class
 * `project_id` isn't in the cached projects list, so its folder is unknown.
 */
export type ProjectNamesOf = (row: Conversation) => ReadonlySet<string> | null;

/**
 * Build {@link ProjectNamesOf} from the cached projects list, dual-reading the
 * first-class `project_id` and the legacy `omni_project` label as the sidebar does.
 *
 * @param projects - The cached `["projects"]` list, if loaded.
 */
export function projectNamesResolver(
  projects: readonly { id: string | null; name: string }[] | undefined,
): ProjectNamesOf {
  const namesById = new Map<string, string>();
  for (const project of projects ?? []) {
    if (project.id !== null) namesById.set(project.id, project.name);
  }
  return (row) => {
    const names = new Set<string>();
    const label = row.labels?.[PROJECT_LABEL_KEY];
    if (label) names.add(label);
    if (row.project_id) {
      const name = namesById.get(row.project_id);
      if (name === undefined) return null;
      names.add(name);
    }
    return names;
  };
}

/** Whether `row` would be listed by `list`, as far as its row fields can tell. */
function couldBelong(
  list: CachedList,
  row: Conversation,
  projectNamesOf: ProjectNamesOf,
  viewerId: string | null,
): boolean {
  const { filters } = list;
  if (row.parent_session_id != null || row.labels?.[SIDE_CHAT_LABEL_KEY] !== undefined) {
    return false;
  }
  if (filters.visibility === "archived") {
    if (row.archived !== true) return false;
  } else if (row.archived === true && (!filters.includeArchived || filters.visibility)) {
    return false;
  }
  // Same inference the sidebar scopes use, so a signed-out solo server still owns its rows.
  const owned = sessionVisibility(row, viewerId) === "mine";
  // Folders list only the viewer's own sessions, whatever their scope.
  if ((filters.visibility === "mine" || list.folder !== undefined) && !owned) return false;
  if (filters.visibility === "shared" && owned) return false;
  const project = list.folder ?? filters.project;
  if (project) {
    const names = projectNamesOf(row);
    if (names !== null && !names.has(project)) return false;
  }
  // A search also matches content no frame carries, so it may hold any row.
  return true;
}

/** Membership fields a list's server-side filter reads. */
function fieldsReadBy(list: CachedList): Set<MembershipField> {
  const fields = new Set<MembershipField>(["archived", "hidden"]);
  if (list.folder !== undefined || list.filters.project) fields.add("project");
  // No sidebar list is keyed by a search today; this keeps one correct if added.
  if (list.filters.searchQuery) fields.add("title");
  return fields;
}

/**
 * Pick the lists a frame's changes need re-read from the server.
 *
 * A list that holds a changed row refetches when the patch said so (the row
 * left it, or it is the next page's cursor row and moved), or when it is a
 * folder the row was just moved out of, so the folder backfills. A list that
 * doesn't hold the row refetches when the change was to a field the list
 * filters on and the row may now belong: an unarchive, a move into the
 * folder's project. A row no list holds yet (a new session) is checked against
 * every list's filters.
 *
 * @param lists - Every cached list, after the frame was patched in.
 * @param changes - Rows whose membership fields changed, by id.
 * @param newRows - Rows no list held before this frame, by id.
 * @param projectNamesOf - Resolves a row's project folders.
 * @param viewerId - The signed-in user, for the ownership scopes.
 * @returns Query hashes to invalidate.
 */
export function listReconcileTargets({
  lists,
  changes,
  newRows,
  projectNamesOf,
  viewerId,
}: {
  lists: readonly CachedList[];
  changes: ReadonlyMap<string, MembershipChange>;
  newRows: ReadonlyMap<string, Conversation>;
  projectNamesOf: ProjectNamesOf;
  viewerId: string | null;
}): Set<string> {
  const targets = new Set<string>();
  for (const list of lists) {
    if (list.needsRefetch) {
      targets.add(list.queryHash);
      continue;
    }
    const reads = fieldsReadBy(list);
    const needed =
      [...changes].some(([id, { row, fields }]) => {
        if (![...fields].some((field) => reads.has(field))) return false;
        if (!list.ids.has(id)) return couldBelong(list, row, projectNamesOf, viewerId);
        // The row moved out of this folder; refetch to fill its slot.
        if (list.folder === undefined || !fields.has("project")) return false;
        const names = projectNamesOf(row);
        return names === null || !names.has(list.folder);
      }) ||
      [...newRows].some(
        ([id, row]) => !list.ids.has(id) && couldBelong(list, row, projectNamesOf, viewerId),
      );
    if (needed) targets.add(list.queryHash);
  }
  return targets;
}
