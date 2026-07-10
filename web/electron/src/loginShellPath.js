"use strict";
const child_process = require("child_process");
const osModule = require("os");

// Markers bracket the PATH value in the shell's stdout so a banner / MOTD /
// version-manager greeting printed by an rc file can't corrupt what we parse.
const START = "__OMNIGENT_PATH_START__";
const END = "__OMNIGENT_PATH_END__";

// Strip ANSI escape sequences (color codes, etc.) that an rc file may emit,
// before treating the captured bytes as a PATH. Matches a CSI sequence —
// ESC "[" , zero or more parameter/intermediate bytes, one final byte — plus the
// two-byte ESC-form used for simpler escapes. ESC is built from its char code so
// no raw control character lives in this source file.
const ESC = String.fromCharCode(27);
const ANSI_RE = new RegExp(ESC + "(?:\\[[0-9;?]*[ -/]*[@-~]|[@-Z\\\\-_])", "g");

function stripAnsi(value) {
  return value.replace(ANSI_RE, "");
}

// Prefer the shell recorded in the passwd DB (reliable even when the process was
// launched from a GUI, where $SHELL is typically unset — the whole premise of
// this bug). Fall back to $SHELL, then the macOS default.
function pickShell(os, env) {
  let shell = null;
  try {
    shell = os.userInfo().shell;
  } catch {
    shell = null;
  }
  return shell || env.SHELL || "/bin/zsh";
}

function extractPath(stdout) {
  if (typeof stdout !== "string") return null;
  const start = stdout.indexOf(START);
  if (start === -1) return null;
  const from = start + START.length;
  const end = stdout.indexOf(END, from);
  if (end === -1) return null;
  const cleaned = stripAnsi(stdout.slice(from, end)).trim();
  return cleaned || null;
}

// On macOS the stripped launchd PATH is /usr/bin:/bin:/usr/sbin:/sbin — Homebrew
// dirs only appear once a login shell has run. Their presence means PATH is
// already complete, so we can skip the shell spawn entirely (e.g. launched from
// a terminal). Conservative by design: when unsure we do the work rather than
// risk skipping and reintroducing the stripped-PATH bug.
function isLikelyLoginPath(env, platform) {
  if (platform !== "darwin") return false;
  return (env.PATH || "")
    .split(":")
    .some((dir) => dir === "/opt/homebrew/bin" || dir === "/usr/local/bin");
}

/**
 * Resolve the login-shell PATH by spawning the user's shell as an
 * interactive+login shell and reading a delimited `$PATH`.
 *
 * `-ilc` sources BOTH the profile and the rc file (zsh: .zprofile + .zshrc,
 * bash: .bash_profile + .bashrc). A login-only shell (`-l`) skips the rc file,
 * where nvm/pyenv/etc. usually export PATH — so `-l` alone would miss exactly
 * the tools this fix targets.
 *
 * Returns the resolved PATH string, or null when nothing needs to change
 * (Windows, PATH already complete, or every candidate shell failed). The caller
 * decides whether/how to merge it into process.env.PATH.
 *
 * Dependencies are injectable so the real module can be unit-tested without
 * spawning a shell.
 *
 * @param {{ execFileSync?: Function, os?: object, env?: object, platform?: string }} [deps]
 * @returns {string | null}
 */
function resolveLoginShellPath(deps = {}) {
  const execFileSync = deps.execFileSync || child_process.execFileSync;
  const os = deps.os || osModule;
  const env = deps.env || process.env;
  const platform = deps.platform || process.platform;

  // Windows GUI processes inherit a full PATH; nothing to do.
  if (platform === "win32") return null;

  // Fast path: PATH already looks like a login-shell PATH — skip the spawn.
  if (isLikelyLoginPath(env, platform)) return null;

  const primary = pickShell(os, env);
  // Try the user's shell first, then POSIX fallbacks. Covers a non-POSIX login
  // shell (nu/fish) where our `-ilc printf` line would not run.
  const shells = [primary, "/bin/zsh", "/bin/bash", "/bin/sh"].filter(
    (shell, i, all) => shell && all.indexOf(shell) === i,
  );

  const args = ["-ilc", `printf '%s' "${START}\${PATH}${END}"`];

  // Spread the real env so rc files that reference $HOME/$USER still work, but
  // suppress startup hooks that can hang the spawn (oh-my-zsh auto-update, the
  // zsh tmux plugin) or paginate output past the timeout.
  const childEnv = {
    ...env,
    DISABLE_AUTO_UPDATE: "true",
    ZSH_TMUX_AUTOSTARTED: "true",
    ZSH_TMUX_AUTOSTART: "false",
    GIT_PAGER: "cat",
    PAGER: "cat",
  };

  for (const shell of shells) {
    try {
      const stdout = execFileSync(shell, args, {
        encoding: "utf8",
        timeout: 5000,
        env: childEnv,
      });
      const resolved = extractPath(stdout);
      if (resolved) return resolved;
    } catch (err) {
      // An interactive+login shell can exit non-zero (rc warnings, no tty) yet
      // still have printed our delimited PATH before failing. execFileSync
      // attaches the captured stdout to the error, so try to recover it before
      // moving on to the next candidate shell.
      const recovered = err && extractPath(err.stdout);
      if (recovered) return recovered;
    }
  }
  return null;
}

/**
 * Union two PATH strings, login PATH taking priority, de-duplicating entries.
 * Preserves any directory the current process had that the login shell lacks
 * (rare, but e.g. an app-injected dir), so this is a real merge rather than a
 * replace.
 *
 * @param {string} currentPath
 * @param {string} loginPath
 * @returns {string}
 */
function mergePath(currentPath, loginPath) {
  const seen = new Set();
  const out = [];
  for (const part of [loginPath, currentPath]) {
    if (!part) continue;
    for (const dir of part.split(":")) {
      if (dir && !seen.has(dir)) {
        seen.add(dir);
        out.push(dir);
      }
    }
  }
  return out.join(":");
}

module.exports = {
  resolveLoginShellPath,
  mergePath,
  // Exported for unit tests.
  extractPath,
  isLikelyLoginPath,
  stripAnsi,
};
