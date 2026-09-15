import { z } from "zod";
import { readRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { readLastAgentId } from "./agentPreferences";
import { readDefaultBaseBranch } from "./baseBranchPreferences";
import { readLastHarness } from "./harnessPreferences";
import { getOmnigentServerIdentity } from "./host";
import { readLastHostChoice, readLastSandboxProvider } from "./hostPreferences";
import { getCurrentUserId } from "./identity";
import { readHarnessOptions } from "./modePreferences";
import { nativeCodingAgentForAvailableAgent } from "./nativeCodingAgents";
import { readAlwaysUseWorktree } from "./worktreeDefaultPreferences";

const MAX_AGE_MS = 24 * 60 * 60 * 1000;
const agentSchema = z.object({ name: z.string(), harness: z.string().nullable() });
const previewSchema = z.object({
  agent: agentSchema,
  label: z.string(),
  model: z.string(),
  effort: z.string(),
  smartRouting: z.boolean(),
});
const permissionPreviewSchema = z.object({
  agent: agentSchema,
  row: z.object({ label: z.string(), value: z.string() }).nullable(),
});
const workspacePreviewSchema = z.object({
  hostId: z.string(),
  workspace: z.string(),
  repositoryLabel: z.string(),
  branchLabel: z.string(),
  branchDescription: z.string(),
});
const pickerAgentSchema = agentSchema.extend({
  id: z.string(),
  display_name: z.string(),
  description: z.string().nullable(),
  skills: z.array(z.object({ name: z.string(), description: z.string() })),
  builtin: z.boolean().optional(),
  acpHarness: z.boolean().optional(),
});
const modelOptionSchema = z.object({
  id: z.string(),
  model: z.string().optional(),
  displayName: z.string().optional(),
  isDefault: z.boolean().optional(),
  defaultReasoningEffort: z.string().optional(),
  supportedReasoningEfforts: z
    .array(z.object({ reasoningEffort: z.string(), description: z.string().optional() }))
    .optional(),
  source: z
    .object({
      kind: z.string(),
      label: z.string(),
      name: z.string().optional(),
      host: z.string().optional(),
    })
    .optional(),
});
const pickerOptionsSchema = z.object({
  agent: pickerAgentSchema,
  agents: z.array(pickerAgentSchema),
  hostId: z.string().nullable(),
  sandboxSelected: z.boolean(),
  model: z.string(),
  models: z.object({
    claude: z.array(modelOptionSchema),
    codex: z.array(modelOptionSchema),
    pi: z.array(modelOptionSchema),
    antigravity: z.array(modelOptionSchema).optional(),
  }),
});

// Cached menus are editable; live queries still own availability and launch readiness.
export type NewChatPickerPreview = z.infer<typeof previewSchema>;
export type NewChatPermissionPreview = z.infer<typeof permissionPreviewSchema>;
export type NewChatWorkspacePreview = z.infer<typeof workspacePreviewSchema>;
export type NewChatPickerOptions = z.infer<typeof pickerOptionsSchema>;

export function getNewChatPickerCacheKey(project?: string, userId?: string | null): string | null {
  if (project === undefined) return null;
  const server = getOmnigentServerIdentity();
  const user = userId === undefined ? getCurrentUserId() : userId;
  if (server === null || user === null) return null;
  return `omnigent:new-chat-picker:v2:${JSON.stringify([server, user, project])}`;
}

function preferenceSignature(preview: { agent: NewChatPickerPreview["agent"] }): string {
  const harness =
    nativeCodingAgentForAvailableAgent(preview.agent)?.harness ?? preview.agent.harness;
  return JSON.stringify([
    readLastAgentId(),
    readLastHarness(readLastAgentId()),
    readLastHostChoice(),
    readLastSandboxProvider(),
    Object.entries(readHarnessOptions(harness)).sort(([a], [b]) => a.localeCompare(b)),
  ]);
}

function workspacePreferenceSignature(preview: NewChatWorkspacePreview): string {
  return JSON.stringify([
    readLastHostChoice(),
    readLastSandboxProvider(),
    readRecentWorkspaces(preview.hostId)[0] ?? null,
    readAlwaysUseWorktree(),
    readDefaultBaseBranch(),
  ]);
}

function readPreview<T>(
  key: string | null,
  schema: z.ZodType<T>,
  signature: (preview: T) => string,
): T | null {
  if (key === null || typeof window === "undefined") return null;
  try {
    const cacheSchema = z.object({ preview: schema, preferences: z.string(), savedAt: z.number() });
    const parsed = cacheSchema.safeParse(JSON.parse(window.localStorage.getItem(key) ?? "null"));
    if (!parsed.success) return null;
    const { preview, savedAt, preferences } = parsed.data;
    const age = Date.now() - savedAt;
    if (age < 0 || age > MAX_AGE_MS || preferences !== signature(preview)) return null;
    return preview;
  } catch {
    return null;
  }
}

function writePreview<T>(
  key: string | null,
  preview: T | null,
  signature: (preview: T) => string,
): void {
  if (key === null || typeof window === "undefined") return;
  try {
    if (preview === null) {
      window.localStorage.removeItem(key);
      return;
    }
    window.localStorage.setItem(
      key,
      JSON.stringify({ preview, preferences: signature(preview), savedAt: Date.now() }),
    );
  } catch {
    // Storage can be unavailable or full; the live picker still works.
  }
}

export function readNewChatPickerCache(key: string | null): NewChatPickerPreview | null {
  return readPreview(key, previewSchema, preferenceSignature);
}

export function writeNewChatPickerCache(
  key: string | null,
  preview: NewChatPickerPreview | null,
): void {
  writePreview(key, preview, preferenceSignature);
}

export function readNewChatPickerOptionsCache(key: string | null): NewChatPickerOptions | null {
  return readPreview(
    key === null ? null : `${key}:options`,
    pickerOptionsSchema,
    preferenceSignature,
  );
}

export function writeNewChatPickerOptionsCache(
  key: string | null,
  options: NewChatPickerOptions | null,
): void {
  writePreview(key === null ? null : `${key}:options`, options, preferenceSignature);
}

export function readNewChatPermissionCache(key: string | null): NewChatPermissionPreview | null {
  return readPreview(
    key === null ? null : `${key}:permissions`,
    permissionPreviewSchema,
    preferenceSignature,
  );
}

export function writeNewChatPermissionCache(
  key: string | null,
  preview: NewChatPermissionPreview | null,
): void {
  writePreview(key === null ? null : `${key}:permissions`, preview, preferenceSignature);
}

export function readNewChatWorkspaceCache(key: string | null): NewChatWorkspacePreview | null {
  return readPreview(
    key === null ? null : `${key}:workspace`,
    workspacePreviewSchema,
    workspacePreferenceSignature,
  );
}

export function writeNewChatWorkspaceCache(
  key: string | null,
  preview: NewChatWorkspacePreview | null,
): void {
  writePreview(key === null ? null : `${key}:workspace`, preview, workspacePreferenceSignature);
}
