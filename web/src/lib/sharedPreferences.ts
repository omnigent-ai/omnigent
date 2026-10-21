import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export type PreferenceValue = string | boolean | string[] | null;
export interface PreferenceKey {
  kind: "model" | "harness" | "harness_args" | "plugin";
  name: string | { ecosystem: string; source: string; name: string };
}
export interface PreferenceIdentity {
  authority_id: string;
  user_id: string;
  workspace_id: string;
}
export interface PreferenceEntry {
  key: PreferenceKey;
  version: number;
  value: PreferenceValue;
}
export interface PreferenceOperation {
  protocol_version: 1;
  identity: PreferenceIdentity;
  installation_id: string;
  operation_id: string;
  key: PreferenceKey;
  expected_version: number;
  value: PreferenceValue;
  resolves: string[];
}
export interface PreferenceSnapshot {
  identity: PreferenceIdentity;
  revision: number;
  entries: PreferenceEntry[];
  conflicts: PreferenceOperation[];
}
export interface PreferencePreview {
  identity: PreferenceIdentity;
  changes: { previous: PreferenceEntry; operation: PreferenceOperation }[];
}
export interface HostPreferences {
  connected: boolean;
  shared: PreferenceSnapshot | null;
  machine: PreferenceEntry[];
  pending: PreferenceOperation[];
  preview: PreferencePreview | null;
  warning: string | null;
}
export type HostPreferenceAction =
  | { action: "status" | "connect" | "sync" | "preview_restore" }
  | { action: "preview_import"; plugins: ("claude" | "codex")[] }
  | { action: "apply_import"; preview: PreferencePreview }
  | { action: "set_machine"; key: PreferenceKey; value: PreferenceValue }
  | { action: "discard"; operation_id: string };

export function preferenceKeyId(key: PreferenceKey): string {
  return JSON.stringify([
    key.kind,
    typeof key.name === "string" ? key.name : [key.name.ecosystem, key.name.source, key.name.name],
  ]);
}
export function preferenceLabel(key: PreferenceKey): string {
  if (typeof key.name !== "string")
    return `${key.name.ecosystem}: ${key.name.name} (${key.name.source})`;
  if (key.kind === "harness_args") return `${key.name} arguments`;
  if (key.kind === "harness") return "Default harness";
  return key.name === "opencode_model" ? "OpenCode model" : "Model";
}
export function preferenceValueLabel(value: PreferenceValue): string {
  if (value === null) return "Inherit";
  if (value === true) return "Enabled";
  if (value === false) return "Disabled";
  if (Array.isArray(value))
    return value.length
      ? value.map((arg) => JSON.stringify(arg)).join(" ")
      : "No default arguments";
  return value;
}

// Pages depend on the preceding revision-bound cursor.
/* eslint-disable no-await-in-loop */
export async function readSharedPreferences(): Promise<PreferenceSnapshot> {
  let snapshot: PreferenceSnapshot | undefined;
  let cursor: string | null = null;
  const cursors = new Set<string>();
  const entries = new Set<string>();
  const conflicts = new Set<string>();
  do {
    const response = await authenticatedFetch(
      `/v1/preferences/shared${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
    );
    if (!response.ok) throw await apiErrorFromResponse(response);
    const page = (await response.json()) as PreferenceSnapshot & { next_cursor: string | null };
    if (!snapshot) snapshot = { ...page, entries: [], conflicts: [] };
    if (
      JSON.stringify(page.identity) !== JSON.stringify(snapshot.identity) ||
      page.revision !== snapshot.revision
    ) {
      throw new Error("Preferences changed while loading. Retry to get a complete snapshot.");
    }
    for (const entry of page.entries) {
      const id = preferenceKeyId(entry.key);
      if (entries.has(id)) throw new Error("Duplicate preference in server response.");
      entries.add(id);
    }
    for (const conflict of page.conflicts) {
      if (conflicts.has(conflict.operation_id))
        throw new Error("Duplicate conflict in server response.");
      conflicts.add(conflict.operation_id);
    }
    snapshot.entries.push(...page.entries);
    snapshot.conflicts.push(...page.conflicts);
    if (snapshot.entries.length > 512 || snapshot.conflicts.length > 128)
      throw new Error("Preference response exceeds its limits.");
    cursor = page.next_cursor;
    if (cursor && (cursors.has(cursor) || cursors.size >= 640))
      throw new Error("Invalid preference continuation.");
    if (cursor) cursors.add(cursor);
  } while (cursor);
  return snapshot;
}

/* eslint-enable no-await-in-loop */

export async function writeSharedPreference(
  operation: PreferenceOperation,
): Promise<{ status: "applied" | "unchanged" | "conflict" }> {
  const response = await authenticatedFetch("/v1/preferences/shared/operations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(operation),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export async function hostPreferenceAction(
  hostId: string,
  action: HostPreferenceAction,
): Promise<HostPreferences> {
  const response = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}/preferences`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(action),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}
