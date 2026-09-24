"use strict";

/**
 * In-app Omnigent CLI installation (macOS).
 *
 * Runs the repo's bundled `install_oss.sh --non-interactive`, which resolves to
 * `uv tool install --force --python 3.12 omnigent` and drops the `omnigent` /
 * `omni` binaries in ~/.local/bin. Streams the installer's live output so the
 * setup page can show it in a terminal pane, mirroring the local-server and
 * Arca-connect flows.
 *
 * `install_oss.sh --non-interactive` *declines* its own uv-bootstrap prompt and
 * fails if uv is missing, so we ensure uv first (the same one-liner the script
 * would have offered), then run the script — which now finds uv and proceeds.
 *
 * The login-shell PATH is already merged into process.env.PATH at startup (see
 * main.js), so spawned children see ~/.local/bin, Homebrew, etc. This module is
 * main-process-free: the spawn is injected so it's unit-testable without a real
 * shell or network.
 */

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

/** Installing pulls uv + a Python toolchain + the package; give it minutes. */
const INSTALL_TIMEOUT_MS = 10 * 60 * 1000;

/** uv's official installer — the same command install_oss.sh would offer. */
const UV_INSTALLER = "curl -LsSf https://astral.sh/uv/install.sh | sh";

/**
 * Locate the bundled `install_oss.sh`. Packaged builds ship it under the app's
 * resources (electron-builder `extraResources`); an unpackaged dev run reads it
 * from the repo `scripts/` dir. Null when neither exists.
 *
 * @param {{ resourcesPath?: string, dirname?: string }} [deps]
 * @returns {string | null}
 */
function resolveInstallScript(deps = {}) {
  const resourcesPath = deps.resourcesPath ?? process.resourcesPath;
  const dirname = deps.dirname ?? __dirname;
  const candidates = [
    resourcesPath ? path.join(resourcesPath, "install_oss.sh") : null,
    // Dev: web/electron/src -> repo root scripts/install_oss.sh
    path.join(dirname, "..", "..", "..", "scripts", "install_oss.sh"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch {
      // Not here; try the next candidate.
    }
  }
  return null;
}

/**
 * Run a command to completion, streaming combined output line-ish chunks to
 * `onOutput`. Never rejects — resolves `{ code }` (null on signal/timeout).
 *
 * @param {string} command
 * @param {string[]} args
 * @param {{
 *   spawn?: typeof spawn,
 *   onOutput?: (text: string) => void,
 *   timeoutMs?: number,
 * }} [deps]
 * @returns {Promise<{ code: number | null, timedOut: boolean }>}
 */
function runStreaming(command, args, deps = {}) {
  const spawnFn = deps.spawn || spawn;
  const onOutput = deps.onOutput || (() => {});
  const timeoutMs = deps.timeoutMs ?? INSTALL_TIMEOUT_MS;
  return new Promise((resolve) => {
    let child;
    try {
      child = spawnFn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
      onOutput(`Failed to start: ${error.message}\n`);
      resolve({ code: null, timedOut: false });
      return;
    }
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try {
        child.kill();
      } catch {
        // Already gone.
      }
    }, timeoutMs);
    if (typeof timer.unref === "function") timer.unref();
    child.stdout?.on("data", (chunk) => onOutput(String(chunk)));
    child.stderr?.on("data", (chunk) => onOutput(String(chunk)));
    child.on("error", (error) => {
      clearTimeout(timer);
      onOutput(`Error: ${error.message}\n`);
      resolve({ code: null, timedOut });
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      resolve({ code, timedOut });
    });
  });
}

/**
 * Ensure `uv` is available, installing it via the official one-liner when
 * missing (install_oss.sh --non-interactive won't do this itself).
 *
 * @param {{
 *   spawn?: typeof spawn,
 *   onOutput?: (text: string) => void,
 *   hasUv?: () => boolean,
 * }} [deps]
 * @returns {Promise<{ ok: boolean, error?: string }>}
 */
async function ensureUv(deps = {}) {
  const hasUv = deps.hasUv || (() => commandExists("uv"));
  if (hasUv()) return { ok: true };
  const onOutput = deps.onOutput || (() => {});
  onOutput("Installing uv (required by the Omnigent installer)…\n");
  const run = await runStreaming("sh", ["-c", UV_INSTALLER], {
    spawn: deps.spawn,
    onOutput,
    timeoutMs: 5 * 60 * 1000,
  });
  if (run.code !== 0 || !hasUv()) {
    return {
      ok: false,
      error:
        "Couldn't install uv, which the Omnigent installer requires. " +
        "Install it from https://docs.astral.sh/uv/getting-started/installation/ and try again.",
    };
  }
  return { ok: true };
}

/**
 * True when `name` resolves on the current PATH.
 *
 * @param {string} name
 * @returns {boolean}
 */
function commandExists(name) {
  try {
    require("node:child_process").execFileSync("/bin/sh", ["-c", `command -v ${name}`], {
      stdio: "ignore",
    });
    return true;
  } catch {
    return false;
  }
}

/**
 * Install the Omnigent CLI. Ensures uv, then runs the bundled installer,
 * streaming output. Never rejects — resolves `{ ok, error? }`. macOS only; a
 * non-darwin platform resolves an actionable error.
 *
 * @param {{
 *   platform?: NodeJS.Platform,
 *   spawn?: typeof spawn,
 *   onOutput?: (text: string) => void,
 *   resolveInstallScript?: () => string | null,
 *   ensureUv?: (d: object) => Promise<{ ok: boolean, error?: string }>,
 *   timeoutMs?: number,
 * }} [deps]
 * @returns {Promise<{ ok: boolean, error?: string }>}
 */
async function installCli(deps = {}) {
  const platform = deps.platform ?? process.platform;
  const onOutput = deps.onOutput || (() => {});
  if (platform !== "darwin") {
    return {
      ok: false,
      error: "In-app install is macOS-only for now. Install the CLI from https://omnigent.ai/.",
    };
  }
  const script = (deps.resolveInstallScript || resolveInstallScript)();
  if (!script) {
    return { ok: false, error: "The bundled installer script was not found." };
  }
  const uv = await (deps.ensureUv || ensureUv)({ spawn: deps.spawn, onOutput });
  if (!uv.ok) return uv;

  onOutput("Installing the Omnigent CLI…\n");
  const run = await runStreaming("sh", [script, "--non-interactive"], {
    spawn: deps.spawn,
    onOutput,
    timeoutMs: deps.timeoutMs,
  });
  if (run.timedOut) {
    return { ok: false, error: "The installer timed out. Check your connection and try again." };
  }
  if (run.code !== 0) {
    return { ok: false, error: `The installer exited with code ${run.code ?? "unknown"}.` };
  }
  return { ok: true };
}

module.exports = {
  INSTALL_TIMEOUT_MS,
  UV_INSTALLER,
  resolveInstallScript,
  ensureUv,
  installCli,
};
