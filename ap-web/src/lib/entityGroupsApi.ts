/**
 * Typed client for the `/v1/entity-groups` endpoints.
 *
 * An *entity group* is a named, icon-bearing category for entities, shown in the
 * flow builder's step picker. Built-in groups (Jira/GitHub) are merged in by the
 * backend and read-only; users can create their own and upload a custom icon.
 * Mirrors `omnigent/server/routes/entity_groups.py`.
 */

import { authenticatedFetch } from "./identity";
import { ApiError } from "./sessionsApi";

/** A group, camelCased with ms timestamps. */
export interface ApiEntityGroup {
  id: string;
  name: string;
  /** Bundled-icon key for built-ins (e.g. "jira"), else null. */
  iconKey: string | null;
  /** URL serving an uploaded custom icon, or null. */
  iconUrl: string | null;
  /** Whether this is a read-only code-owned built-in. */
  isBuiltin: boolean;
  /** Epoch ms. */
  createdAt: number;
  updatedAt: number;
}

interface EntityGroupWire {
  id: string;
  name: string;
  icon_key: string | null;
  icon_url: string | null;
  is_builtin: boolean;
  created_at: number;
  updated_at: number;
}

function groupFromWire(w: EntityGroupWire): ApiEntityGroup {
  return {
    id: w.id,
    name: w.name,
    iconKey: w.icon_key ?? null,
    iconUrl: w.icon_url ?? null,
    isBuiltin: !!w.is_builtin,
    createdAt: w.created_at * 1000,
    updatedAt: w.updated_at * 1000,
  };
}

async function readJsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    let code: string | null = null;
    try {
      const body = (await res.json()) as { error?: { code?: string; message?: string } };
      if (body.error?.message) message = body.error.message;
      if (body.error?.code) code = body.error.code;
    } catch {
      // Non-JSON / empty body — keep the status-line fallback.
    }
    throw new ApiError(message, res.status, code);
  }
  return (await res.json()) as T;
}

/** Payload accepted by {@link apiCreateEntityGroup} / {@link apiUpdateEntityGroup}. */
export interface EntityGroupInput {
  name?: string;
  iconKey?: string | null;
}

function toBody(input: EntityGroupInput): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (input.name !== undefined) body.name = input.name;
  if (input.iconKey !== undefined) body.icon_key = input.iconKey;
  return body;
}

export async function apiListEntityGroups(): Promise<ApiEntityGroup[]> {
  const res = await authenticatedFetch("/v1/entity-groups");
  const wires = await readJsonOrThrow<EntityGroupWire[]>(res);
  return wires.map(groupFromWire);
}

export async function apiCreateEntityGroup(input: EntityGroupInput): Promise<ApiEntityGroup> {
  const res = await authenticatedFetch("/v1/entity-groups", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(toBody(input)),
  });
  return groupFromWire(await readJsonOrThrow<EntityGroupWire>(res));
}

export async function apiUpdateEntityGroup(
  id: string,
  input: EntityGroupInput,
): Promise<ApiEntityGroup> {
  const res = await authenticatedFetch(`/v1/entity-groups/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(toBody(input)),
  });
  return groupFromWire(await readJsonOrThrow<EntityGroupWire>(res));
}

export async function apiDeleteEntityGroup(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/entity-groups/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok && res.status !== 404) {
    throw new ApiError(`${res.status} ${res.statusText}`, res.status, null);
  }
}

/** Upload (or replace) a group's custom icon image; returns the updated group. */
export async function apiUploadEntityGroupIcon(
  id: string,
  file: File,
): Promise<ApiEntityGroup> {
  const form = new FormData();
  form.append("file", file, file.name || "icon.png");
  const res = await authenticatedFetch(`/v1/entity-groups/${encodeURIComponent(id)}/icon`, {
    method: "POST",
    body: form,
  });
  return groupFromWire(await readJsonOrThrow<EntityGroupWire>(res));
}
