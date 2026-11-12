/** macOS NSUserDefaults key in the ai.omnigent.desktop preference domain. */
export const DEVELOPER_MODE_KEY = "DeveloperMode";

export interface DeveloperModeOptions {
  isPackaged: boolean;
  platform?: NodeJS.Platform;
  getUserDefault?: (key: string, type: "boolean") => unknown;
}

/**
 * Whether shell developer affordances should be enabled.
 *
 * Development builds always enable them. Packaged builds require an explicit
 * macOS user default so production debugging cannot be turned on accidentally.
 */
export function isDeveloperModeEnabled({
  isPackaged,
  platform = process.platform,
  getUserDefault,
}: DeveloperModeOptions): boolean {
  if (!isPackaged) return true;
  if (platform !== "darwin" || typeof getUserDefault !== "function") return false;

  try {
    return getUserDefault(DEVELOPER_MODE_KEY, "boolean") === true;
  } catch {
    return false;
  }
}
