/** A workspace resource owned either by a running session or directly by a host. */
export type WorkspaceResourceTarget =
  | string
  | { kind: "session"; sessionId: string }
  | { kind: "host"; hostId: string; workspace: string };

export type NormalizedWorkspaceResourceTarget = Exclude<WorkspaceResourceTarget, string>;

export function normalizeWorkspaceResourceTarget(
  target: WorkspaceResourceTarget | null | undefined,
): NormalizedWorkspaceResourceTarget | undefined {
  if (typeof target === "string") {
    return target ? { kind: "session", sessionId: target } : undefined;
  }
  if (!target) return undefined;
  if (target.kind === "session") {
    return target.sessionId ? target : undefined;
  }
  return target.hostId && target.workspace ? target : undefined;
}

export function workspaceTargetSessionId(
  target: WorkspaceResourceTarget | null | undefined,
): string | undefined {
  const normalized = normalizeWorkspaceResourceTarget(target);
  return normalized?.kind === "session" ? normalized.sessionId : undefined;
}

export function isHostWorkspaceTarget(
  target: WorkspaceResourceTarget | null | undefined,
): target is Extract<NormalizedWorkspaceResourceTarget, { kind: "host" }> {
  return normalizeWorkspaceResourceTarget(target)?.kind === "host";
}

/** Stable pieces used after a resource-specific React Query key prefix. */
export function workspaceTargetKey(
  target: WorkspaceResourceTarget | null | undefined,
): readonly [string] | readonly ["host", string, string] | readonly ["none"] {
  const normalized = normalizeWorkspaceResourceTarget(target);
  if (!normalized) return ["none"];
  return normalized.kind === "session"
    ? [normalized.sessionId]
    : ["host", normalized.hostId, normalized.workspace];
}

/** Build a resource URL while preserving resource-specific query parameters. */
export function workspaceResourceUrl(
  target: WorkspaceResourceTarget,
  resourcePath: string,
  query?: URLSearchParams | Record<string, string | undefined>,
): string {
  const normalized = normalizeWorkspaceResourceTarget(target);
  if (!normalized) throw new Error("Workspace resource target is required");

  const suffix = resourcePath.replace(/^\/+/, "");
  const base =
    normalized.kind === "session"
      ? `/v1/sessions/${encodeURIComponent(normalized.sessionId)}/resources`
      : `/v1/hosts/${encodeURIComponent(normalized.hostId)}/workspace/resources`;
  const params =
    query instanceof URLSearchParams
      ? new URLSearchParams(query)
      : new URLSearchParams(
          Object.entries(query ?? {}).filter(
            (entry): entry is [string, string] => entry[1] !== undefined,
          ),
        );
  if (normalized.kind === "host") params.set("workspace", normalized.workspace);
  const encoded = params.toString();
  return `${base}${suffix ? `/${suffix}` : ""}${encoded ? `?${encoded}` : ""}`;
}
