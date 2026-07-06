"use strict";
const { execFileSync } = require("child_process");

/**
 * Resolve the login-shell PATH by spawning `$SHELL -l -c 'echo $PATH'`.
 * Returns the trimmed PATH string on success, or null if the shell spawn fails.
 * The caller is responsible for deciding whether to update process.env.PATH.
 */
function resolveLoginShellPath() {
  const shell = process.env.SHELL || "/bin/bash";
  try {
    const result = execFileSync(shell, ["-l", "-c", "echo $PATH"], {
      encoding: "utf8",
      timeout: 5000,
    }).trim();
    return result || null;
  } catch {
    return null;
  }
}

module.exports = { resolveLoginShellPath };
