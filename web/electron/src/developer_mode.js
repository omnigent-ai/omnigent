"use strict";

/** macOS NSUserDefaults key in the ai.omnigent.desktop preference domain. */
const DEVELOPER_MODE_KEY = "DeveloperMode";

/**
 * Whether shell developer affordances should be enabled.
 *
 * Development builds always enable them. Packaged builds require an explicit
 * macOS user default so production debugging cannot be turned on accidentally.
 *
 * @param {{
 *   isPackaged: boolean,
 *   platform?: NodeJS.Platform,
 *   getUserDefault?: (key: string, type: string) => unknown,
 * }} options
 * @returns {boolean}
 */
function isDeveloperModeEnabled({ isPackaged, platform = process.platform, getUserDefault }) {
  if (!isPackaged) return true;
  if (platform !== "darwin" || typeof getUserDefault !== "function") return false;

  try {
    return getUserDefault(DEVELOPER_MODE_KEY, "boolean") === true;
  } catch {
    return false;
  }
}

module.exports = { DEVELOPER_MODE_KEY, isDeveloperModeEnabled };
