/**
 * Entity group store (server-backed).
 *
 * A *group* is a named, icon-bearing category for entities, shown in the flow
 * builder's step picker. Built-in groups (Jira/GitHub) come from the backend and
 * are read-only; users can create their own and upload a custom icon.
 *
 * Persistence is the `/v1/entity-groups` API (see {@link entityGroupsApi}); this
 * wraps it with an in-memory cache so the React hooks read synchronously and
 * re-render on change — the same pattern as {@link entityStore}.
 */

import { useCallback, useEffect, useState } from "react";
import {
  apiCreateEntityGroup,
  apiDeleteEntityGroup,
  apiListEntityGroups,
  apiUpdateEntityGroup,
  apiUploadEntityGroupIcon,
  type ApiEntityGroup,
} from "@/lib/entityGroupsApi";

export type EntityGroup = ApiEntityGroup;

/** Cross-component change signal so the hooks re-read after a mutation. */
const EVENT = "omnigent-entity-groups-changed";

const cache = new Map<string, EntityGroup>();
let listLoaded = false;

function emit(): void {
  window.dispatchEvent(new Event(EVENT));
}

function put(g: EntityGroup): EntityGroup {
  cache.set(g.id, g);
  return g;
}

/** Built-ins first (createdAt 0), then user groups newest-updated first. */
function cachedList(): EntityGroup[] {
  return [...cache.values()].sort((a, b) => {
    if (a.isBuiltin !== b.isBuiltin) return a.isBuiltin ? -1 : 1;
    return b.updatedAt - a.updatedAt;
  });
}

async function refreshList(): Promise<void> {
  const groups = await apiListEntityGroups();
  cache.clear();
  for (const g of groups) put(g);
  listLoaded = true;
  emit();
}

export function listEntityGroups(): EntityGroup[] {
  return cachedList();
}

export async function createEntityGroup(name: string): Promise<EntityGroup> {
  const g = put(await apiCreateEntityGroup({ name: name.trim() || "Untitled group" }));
  emit();
  return g;
}

export async function updateEntityGroup(id: string, patch: { name?: string }): Promise<void> {
  put(await apiUpdateEntityGroup(id, patch));
  emit();
}

export async function deleteEntityGroup(id: string): Promise<void> {
  await apiDeleteEntityGroup(id);
  cache.delete(id);
  emit();
}

/** Upload a custom icon for a group; updates the cache with the new iconUrl. */
export async function uploadEntityGroupIcon(id: string, file: File): Promise<void> {
  put(await apiUploadEntityGroupIcon(id, file));
  emit();
}

/**
 * Reactive group list — loads from the API on first use and re-reads from the
 * cache on any change (via the `omnigent-entity-groups-changed` event).
 */
export function useEntityGroups(): EntityGroup[] {
  const [groups, setGroups] = useState<EntityGroup[]>(() => cachedList());
  const refresh = useCallback(() => setGroups(cachedList()), []);
  useEffect(() => {
    window.addEventListener(EVENT, refresh);
    if (!listLoaded) void refreshList().catch(() => {});
    else refresh();
    return () => window.removeEventListener(EVENT, refresh);
  }, [refresh]);
  return groups;
}
