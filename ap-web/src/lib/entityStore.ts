/**
 * Entity store (server-backed).
 *
 * An *entity* is a reusable `{ id, title, instruction }` building block wired
 * into flows as a step (e.g. the Jira actions). The `instruction` is the text
 * folded into a flow's narrative when the entity is used.
 *
 * Persistence is the `/v1/entities` API (see {@link entitiesApi}); this module
 * wraps it with a small in-memory cache so the React hooks read synchronously
 * and re-render on change — the same pattern as {@link jobsStore}.
 *
 * History: this was browser-localStorage-only in the flows UI prototype. It now
 * talks to the backend so entities are shared and real.
 */

import { useCallback, useEffect, useState } from "react";
import {
  apiCreateEntity,
  apiDeleteEntity,
  apiListEntities,
  apiUpdateEntity,
  type ApiEntity,
} from "@/lib/entitiesApi";

export interface Entity {
  id: string;
  title: string;
  /** Text folded into the flow narrative when this entity is used as a step. */
  instruction: string;
  /** Owning group id, or null if ungrouped. */
  groupId: string | null;
  /** Whether this is a read-only code-owned built-in (e.g. a Jira action). */
  isBuiltin: boolean;
  /** Epoch ms. */
  createdAt: number;
  updatedAt: number;
}

/** Cross-component change signal so the hooks re-read after a mutation. */
const EVENT = "omnigent-entities-changed";

/** In-memory cache so `useEntities` can read synchronously. */
const cache = new Map<string, Entity>();
let listLoaded = false;

function emit(): void {
  window.dispatchEvent(new Event(EVENT));
}

function fromApi(e: ApiEntity): Entity {
  return {
    id: e.id,
    title: e.title,
    instruction: e.instruction,
    groupId: e.groupId,
    isBuiltin: e.isBuiltin,
    createdAt: e.createdAt,
    updatedAt: e.updatedAt,
  };
}

function put(e: Entity): Entity {
  cache.set(e.id, e);
  return e;
}

/** Newest-updated first. */
function cachedList(): Entity[] {
  return [...cache.values()].sort((a, b) => b.updatedAt - a.updatedAt);
}

async function refreshList(): Promise<void> {
  // Built-in entities (Jira/GitHub actions) are now code-owned on the backend
  // and returned by the list endpoint — no client-side seeding.
  const entities = await apiListEntities();
  cache.clear();
  for (const e of entities) put(fromApi(e));
  listLoaded = true;
  emit();
}

export function listEntities(): Entity[] {
  return cachedList();
}

export function getEntity(id: string): Entity | undefined {
  return cache.get(id);
}

/** Create a new entity, optionally in a group. Returns it. */
export async function createEntity(
  title: string,
  instruction: string,
  groupId?: string | null,
): Promise<Entity> {
  const e = put(
    fromApi(
      await apiCreateEntity({ title: title.trim() || "Untitled", instruction, groupId }),
    ),
  );
  emit();
  return e;
}

/** Patch title, instruction, and/or group (groupId: null clears the group). */
export async function updateEntity(
  id: string,
  patch: { title?: string; instruction?: string; groupId?: string | null },
): Promise<void> {
  const e = fromApi(await apiUpdateEntity(id, patch));
  put(e);
  emit();
}

export async function deleteEntity(id: string): Promise<void> {
  await apiDeleteEntity(id);
  cache.delete(id);
  emit();
}

/**
 * Reactive entity list — loads from the API on first use and re-reads from the
 * cache on any create/update/delete (via the `omnigent-entities-changed` event).
 */
export function useEntities(): Entity[] {
  const [entities, setEntities] = useState<Entity[]>(() => cachedList());
  const refresh = useCallback(() => setEntities(cachedList()), []);
  useEffect(() => {
    window.addEventListener(EVENT, refresh);
    // Load the list once across the app; subsequent mounts read the cache.
    if (!listLoaded) void refreshList().catch(() => {});
    else refresh();
    return () => window.removeEventListener(EVENT, refresh);
  }, [refresh]);
  return entities;
}
