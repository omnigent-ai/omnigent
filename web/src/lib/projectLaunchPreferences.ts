// Per-project memory of how the project's last session started (agent, harness
// pick and options, host, working directory), keyed by project id and ignored
// once the project's stored settings change after that start.

import { z } from "zod";
import { AUTO_NATIVE_HARNESS_ID } from "./agentLabels";
import type { HarnessOptions } from "./modePreferences";
import type { ProjectConfig } from "./projectsApi";

const STORAGE_KEY = "omnigent:project-launch-defaults";

const entrySchema = z.object({
  agentId: z.string().optional(),
  pickedHarness: z.string().nullable().optional(),
  harness: z.string().optional(),
  harnessOptions: z.record(z.string(), z.string()).optional(),
  hostId: z.string().optional(),
  workspace: z.string().optional(),
  configSignature: z.string(),
});

/** The settings a project's last session started with. */
export interface ProjectLaunchDefaults {
  /** Agent the session ran. */
  agentId?: string;
  /** The composer's harness pick for that agent (Smart Routing, or a bundle
   *  agent's brain harness); `null` = the agent's own harness. */
  pickedHarness?: string | null;
  /** Native harness the agent ran on, with the options it launched with, in
   *  the per-harness shape modePreferences remembers (`""` = harness default). */
  harness?: string;
  harnessOptions?: HarnessOptions;
  /** Host id, or the sandbox sentinel. */
  hostId?: string;
  /** Working directory on that host; unset for a sandbox. */
  workspace?: string;
}

/**
 * A project's stored settings with its last start overlaid, as Project settings
 * shows them. The model and effort come from the start's harness options (`""`
 * = no default). A Smart Routing start keeps the stored agent, since the
 * settings have no field for Smart Routing.
 */
export function projectConfigWithLaunch(
  config: ProjectConfig,
  launch: ProjectLaunchDefaults | null,
): ProjectConfig {
  if (launch === null) return config;
  const merged = { ...config };
  if (launch.agentId !== undefined && launch.pickedHarness !== AUTO_NATIVE_HARNESS_ID) {
    merged.agent_id = launch.agentId;
    if (launch.harnessOptions !== undefined || launch.agentId !== config.agent_id) {
      merged.model = launch.harnessOptions?.model || undefined;
      merged.effort = launch.harnessOptions?.effort || undefined;
    }
  }
  if (launch.hostId !== undefined) {
    merged.host_id = launch.hostId;
    merged.workspace = launch.workspace;
  }
  return merged;
}

/**
 * Fingerprint of a project's stored defaults. The icon is excluded because
 * it doesn't affect how a session starts.
 */
function configSignature(config: ProjectConfig): string {
  return JSON.stringify(
    Object.entries(config)
      .filter(([key, value]) => key !== "icon" && value !== undefined)
      .sort(([a], [b]) => a.localeCompare(b)),
  );
}

/** Map of project id -> stored entry; `{}` when absent or corrupt. */
function readAll(): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null");
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

/**
 * Read how `projectId`'s last session started. Returns `null` when nothing is
 * stored, the entry is malformed, the project's stored defaults (`config`)
 * changed since that start, or storage is inaccessible — never throws.
 */
export function readProjectLaunchDefaults(
  projectId: string,
  config: ProjectConfig,
): ProjectLaunchDefaults | null {
  if (typeof window === "undefined") return null;
  const parsed = entrySchema.safeParse(readAll()[projectId]);
  if (!parsed.success) return null;
  const { configSignature: savedSignature, ...defaults } = parsed.data;
  return savedSignature === configSignature(config) ? defaults : null;
}

/**
 * Remember how a session in `projectId` started, against the project's stored
 * defaults (`config`) at that time. Swallows quota/access errors so a failed
 * write can't break session creation.
 */
export function writeProjectLaunchDefaults(
  projectId: string,
  defaults: ProjectLaunchDefaults,
  config: ProjectConfig,
): void {
  if (typeof window === "undefined") return;
  try {
    const all = readAll();
    all[projectId] = { ...defaults, configSignature: configSignature(config) };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
  } catch {
    // localStorage quota or access errors shouldn't break the composer.
  }
}

const CLEARED_EVENT = "omnigent:project-launch-defaults-cleared";

/**
 * Forget how `projectId`'s last session started, once its settings are saved,
 * and tell open composers to re-read it. Swallows quota/access errors.
 */
export function clearProjectLaunchDefaults(projectId: string): void {
  if (typeof window === "undefined") return;
  try {
    const { [projectId]: _cleared, ...rest } = readAll();
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(rest));
  } catch {
    // localStorage quota or access errors shouldn't break the settings save.
  }
  window.dispatchEvent(new Event(CLEARED_EVENT));
}

/** Call `listener` after any project's last start is cleared; returns an unsubscribe. */
export function onProjectLaunchDefaultsCleared(listener: () => void): () => void {
  window.addEventListener(CLEARED_EVENT, listener);
  return () => window.removeEventListener(CLEARED_EVENT, listener);
}
