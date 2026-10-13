// Client for `/v1/static-sandboxes` (omnigent/server/routes/static_sandboxes.py):
// Static sandboxes, the user's own long-lived sandboxes saved as New Session
// hosts. The list lives with each provider; the server passes it through.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "./identity";
import type { AvailableSandbox, StaticSandbox, StaticSandboxStatus } from "./staticSandboxes";

interface AvailableSandboxWire {
  provider: string;
  sandbox_id: string;
  status: string;
  repo?: string;
  last_activity_at?: string | null;
}

interface StaticSandboxWire extends AvailableSandboxWire {
  label: string;
}

/** A failed static-sandboxes call, carrying the server's message and status. */
export class StaticSandboxesApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "StaticSandboxesApiError";
    this.status = status;
  }
}

async function readJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as {
      error?: { message?: string };
      detail?: unknown;
    };
    const detail = typeof body.detail === "string" ? body.detail : undefined;
    throw new StaticSandboxesApiError(
      body.error?.message ?? detail ?? `${res.status} ${res.statusText}`,
      res.status,
    );
  }
  return (await res.json()) as T;
}

function toStatus(raw: string): StaticSandboxStatus {
  return raw === "running" || raw === "gone" ? raw : "paused";
}

function toAvailableSandbox(wire: AvailableSandboxWire): AvailableSandbox {
  return {
    provider: wire.provider,
    id: wire.sandbox_id,
    status: toStatus(wire.status),
    repo: wire.repo ?? "",
    lastActivityAt: wire.last_activity_at ?? null,
  };
}

function toStaticSandbox(wire: StaticSandboxWire): StaticSandbox {
  return { ...toAvailableSandbox(wire), label: wire.label || wire.sandbox_id };
}

function entryPath(provider: string, sandboxId: string): string {
  return `/v1/static-sandboxes/${encodeURIComponent(provider)}/${encodeURIComponent(sandboxId)}`;
}

/** Every sandbox the user owns at each provider, for the Add picker. */
export async function listAvailableSandboxes(): Promise<AvailableSandbox[]> {
  const res = await authenticatedFetch("/v1/static-sandboxes/available");
  const body = await readJson<{ available_sandboxes: AvailableSandboxWire[] }>(res);
  return body.available_sandboxes.map(toAvailableSandbox);
}

/** The user's saved static sandboxes, with each sandbox's live status. */
export async function listStaticSandboxes(): Promise<StaticSandbox[]> {
  const res = await authenticatedFetch("/v1/static-sandboxes");
  const body = await readJson<{ static_sandboxes: StaticSandboxWire[] }>(res);
  return body.static_sandboxes.map(toStaticSandbox);
}

/** Adds a sandbox as a static sandbox, or renames it (one upsert). */
export async function saveStaticSandbox(
  provider: string,
  sandboxId: string,
  label: string,
): Promise<StaticSandbox> {
  const res = await authenticatedFetch(entryPath(provider, sandboxId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label }),
  });
  return toStaticSandbox(await readJson<StaticSandboxWire>(res));
}

/** Removes the entry and stops its host; the sandbox itself is left to the provider. */
export async function removeStaticSandbox(provider: string, sandboxId: string): Promise<void> {
  const res = await authenticatedFetch(entryPath(provider, sandboxId), { method: "DELETE" });
  if (!res.ok && res.status !== 404) await readJson(res);
}

/** Wakes the sandbox and starts its host ahead of a New Session's Start. */
export async function wakeStaticSandbox(provider: string, sandboxId: string): Promise<void> {
  const res = await authenticatedFetch(`${entryPath(provider, sandboxId)}/wake`, {
    method: "POST",
  });
  await readJson(res);
}

/** Wakes a session's paused host in place (`POST /v1/sessions/{id}/wake`). */
export async function wakeSessionHost(sessionId: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/wake`, {
    method: "POST",
  });
  await readJson(res);
}

export const STATIC_SANDBOXES_KEY = ["static-sandboxes", "saved"] as const;
const AVAILABLE_SANDBOXES_KEY = ["static-sandboxes", "available"] as const;

export function useStaticSandboxes({ enabled }: { enabled: boolean }) {
  return useQuery({
    queryKey: STATIC_SANDBOXES_KEY,
    queryFn: listStaticSandboxes,
    staleTime: 15_000,
    enabled,
  });
}

export function useAvailableSandboxes({ enabled }: { enabled: boolean }) {
  return useQuery({
    queryKey: AVAILABLE_SANDBOXES_KEY,
    queryFn: listAvailableSandboxes,
    staleTime: 15_000,
    enabled,
  });
}

export function useSaveStaticSandbox() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      provider,
      sandboxId,
      label,
    }: {
      provider: string;
      sandboxId: string;
      label: string;
    }) => saveStaticSandbox(provider, sandboxId, label),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: STATIC_SANDBOXES_KEY }),
  });
}

export function useRemoveStaticSandbox() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ provider, sandboxId }: { provider: string; sandboxId: string }) =>
      removeStaticSandbox(provider, sandboxId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: STATIC_SANDBOXES_KEY }),
  });
}
