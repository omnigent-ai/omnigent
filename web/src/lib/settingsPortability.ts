// Export and import user-facing UI settings as a JSON file.
//
// Only keys that represent deliberate user preferences are included — not
// ephemeral state like last-selected agent/harness/host or per-session
// workspace layout. The exported blob is a plain JSON object with a `version`
// field for future compatibility, plus one entry per localStorage key that
// holds a non-default value.

/** localStorage keys that constitute exportable user preferences. */
const EXPORTABLE_KEYS = [
  "omnigent:ui-font-size",
  "omnigent:ui-font-family",
  "omnigent:code-font-size",
  "omnigent:code-font-family",
  "omnigent:code-font-weight",
  "omnigent:terminal-theme",
  "omnigent:ui-theme-palette",
  "omnigent:custom-theme",
  "omnigent:default-workspace-panel",
  "omnigent:default-transcript-view",
  "omnigent:hide-unconfigured-harnesses",
  "omnigent:default-base-branch",
  "omnigent:always-use-worktree",
  "web-theme",
] as const;

const CURRENT_VERSION = 1;

export interface ExportedSettings {
  version: number;
  settings: Record<string, string>;
}

function isExportedSettings(value: unknown): value is ExportedSettings {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ExportedSettings>;
  if (typeof candidate.version !== "number" || candidate.version < 1) return false;
  if (!candidate.settings || typeof candidate.settings !== "object") return false;
  const allowedKeys = new Set<string>(EXPORTABLE_KEYS);
  for (const [key, val] of Object.entries(candidate.settings)) {
    if (!allowedKeys.has(key)) return false;
    if (typeof val !== "string") return false;
  }
  return true;
}

/**
 * Collect all exportable settings from localStorage into a downloadable JSON
 * blob. Exports even when all values are at defaults so that a fully-default
 * device can export and share its baseline.
 */
export function collectSettings(): ExportedSettings | null {
  if (typeof window === "undefined") return null;
  const settings: Record<string, string> = {};
  try {
    for (const key of EXPORTABLE_KEYS) {
      const value = window.localStorage.getItem(key);
      if (value !== null) settings[key] = value;
    }
  } catch {
    return null;
  }
  return { version: CURRENT_VERSION, settings };
}

/** Trigger a browser download of the exported settings as a JSON file. */
export function downloadSettings(exported: ExportedSettings): void {
  const json = JSON.stringify(exported, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = "omnigent-settings.json";
    a.click();
  } finally {
    URL.revokeObjectURL(url);
  }
}

/**
 * Read and validate a settings JSON file chosen by the user. Rejects with a
 * human-readable error message on invalid input.
 */
export function readSettingsFile(file: File): Promise<ExportedSettings> {
  return new Promise((resolve, reject) => {
    if (!file.name.endsWith(".json")) {
      reject(new Error("Please select a .json file."));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const parsed: unknown = JSON.parse(reader.result as string);
        if (!isExportedSettings(parsed)) {
          reject(new Error("The file doesn't contain valid Omnigent settings."));
          return;
        }
        resolve(parsed);
      } catch {
        reject(new Error("The file contains invalid JSON."));
      }
    };
    reader.onerror = () => reject(new Error("Failed to read the file."));
    reader.readAsText(file);
  });
}

/**
 * Write imported settings into localStorage, clearing all exportable keys
 * first to ensure a true overwrite (not merge). Returns the number of keys
 * written.
 */
export function applyImportedSettings(imported: ExportedSettings): number {
  if (typeof window === "undefined") return 0;
  const allowedKeys = new Set<string>(EXPORTABLE_KEYS);

  // Clear all exportable keys first to honor the "overwrite" contract.
  for (const key of EXPORTABLE_KEYS) {
    try {
      window.localStorage.removeItem(key);
    } catch {
      // Swallow individual removal failures.
    }
  }

  // Write imported values.
  let count = 0;
  for (const [key, value] of Object.entries(imported.settings)) {
    if (!allowedKeys.has(key)) continue;
    try {
      window.localStorage.setItem(key, value);
      count++;
    } catch {
      // Swallow individual write failures.
    }
  }
  return count;
}
