/**
 * Typed client for the `/v1/entities` endpoints.
 *
 * An *entity* is a reusable named instruction (e.g. the Jira actions) wired into
 * flows as a step. Mirrors `omnigent/server/routes/entities.py`.
 *
 * Naming: the TS surface is camelCase; the wire is snake_case. Timestamps are
 * epoch **seconds** on the wire and converted to epoch **ms** here so the rest
 * of the app (which formats with `relativeTime`) sees a uniform ms value.
 *
 * All requests go through the existing Vite `/v1` proxy.
 */

import { authenticatedFetch } from "./identity";
import { ApiError } from "./sessionsApi";

/** A saved entity, camelCased with ms timestamps. */
export interface ApiEntity {
  id: string;
  title: string;
  instruction: string;
  /** Owning group id, or null if ungrouped. */
  groupId: string | null;
  /** Whether this is a read-only code-owned built-in. */
  isBuiltin: boolean;
  /** Epoch ms. */
  createdAt: number;
  updatedAt: number;
}

interface EntityWire {
  id: string;
  title: string;
  instruction: string;
  group_id: string | null;
  is_builtin: boolean;
  created_at: number;
  updated_at: number;
}

function entityFromWire(w: EntityWire): ApiEntity {
  return {
    id: w.id,
    title: w.title,
    instruction: w.instruction ?? "",
    groupId: w.group_id ?? null,
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

/** Payload accepted by {@link apiCreateEntity} / {@link apiUpdateEntity}. */
export interface EntityInput {
  title?: string;
  instruction?: string;
  /** Group id to assign; "" moves to ungrouped; undefined leaves unchanged. */
  groupId?: string | null;
}

function toBody(input: EntityInput): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (input.title !== undefined) body.title = input.title;
  if (input.instruction !== undefined) body.instruction = input.instruction;
  // Map null -> "" so the backend clears the group; a real id passes through.
  if (input.groupId !== undefined) body.group_id = input.groupId ?? "";
  return body;
}

export async function apiListEntities(): Promise<ApiEntity[]> {
  const res = await authenticatedFetch("/v1/entities");
  const wires = await readJsonOrThrow<EntityWire[]>(res);
  return wires.map(entityFromWire);
}

export async function apiGetEntity(id: string): Promise<ApiEntity> {
  const res = await authenticatedFetch(`/v1/entities/${encodeURIComponent(id)}`);
  return entityFromWire(await readJsonOrThrow<EntityWire>(res));
}

export async function apiCreateEntity(input: EntityInput): Promise<ApiEntity> {
  const res = await authenticatedFetch("/v1/entities", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(toBody(input)),
  });
  return entityFromWire(await readJsonOrThrow<EntityWire>(res));
}

export async function apiUpdateEntity(id: string, input: EntityInput): Promise<ApiEntity> {
  const res = await authenticatedFetch(`/v1/entities/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(toBody(input)),
  });
  return entityFromWire(await readJsonOrThrow<EntityWire>(res));
}

export async function apiDeleteEntity(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/entities/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok && res.status !== 404) {
    throw new ApiError(`${res.status} ${res.statusText}`, res.status, null);
  }
}
