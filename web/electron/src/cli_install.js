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
const os = require("node:os");
const path = require("node:path");

/** Installing pulls uv + a Python toolchain + the package; give it minutes. */
const INSTALL_TIMEOUT_MS = 10 * 60 * 1000;

/** uv's official installer — the same command install_oss.sh would offer. */
const UV_INSTALLER = "curl -LsSf https://astral.sh/uv/install.sh | sh";

/** The only script name we ever run — never taken from external input. */
const INSTALL_SCRIPT_NAME = "install_oss.sh";

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
    resourcesPath ? path.join(resourcesPath, INSTALL_SCRIPT_NAME) : null,
    // Dev: web/electron/src -> repo root scripts/install_oss.sh
    path.join(dirname, "..", "..", "..", "scripts", INSTALL_SCRIPT_NAME),
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
  // Grace between SIGTERM and the hard SIGKILL escalation on timeout.
  const killGraceMs = deps.killGraceMs ?? 5000;
  return new Promise((resolve) => {
    let child;
    try {
      // `detached` puts the child in its own process group so we can signal the
      // whole tree (kill(-pid)) — the installer spawns uv/curl subprocesses that
      // a bare child.kill() would leave running past a timeout.
      child = spawnFn(command, args, {
        stdio: ["ignore", "pipe", "pipe"],
        detached: true,
        env: deps.env ?? process.env,
      });
    } catch (error) {
      onOutput(`Failed to start: ${error.message}\n`);
      resolve({ code: null, timedOut: false });
      return;
    }
    let settled = false;
    const settle = (result) => {
      if (settled) return;
      settled = true;
      resolve(result);
    };
    // Signal the child's whole process group; fall back to the lone child if the
    // group send fails (e.g. no pid, or the injected fake in tests).
    const killTree = (signal) => {
      try {
        if (typeof child.pid === "number") process.kill(-child.pid, signal);
        else child.kill(signal);
      } catch {
        try {
          child.kill(signal);
        } catch {
          // Already gone.
        }
      }
    };
    let timedOut = false;
    let killTimer;
    const timer = setTimeout(() => {
      timedOut = true;
      killTree("SIGTERM");
      // A signal-resistant process must not keep the operation pending: escalate
      // to SIGKILL, then settle regardless so the caller isn't stuck past the
      // deadline even if no exit event ever arrives.
      // Not unref'd: this timer must fire to escalate + settle a wedged child.
      killTimer = setTimeout(() => {
        killTree("SIGKILL");
        settle({ code: null, timedOut: true });
      }, killGraceMs);
    }, timeoutMs);
    const finish = (result) => {
      clearTimeout(timer);
      clearTimeout(killTimer);
      settle(result);
    };
    child.stdout?.on("data", (chunk) => onOutput(String(chunk)));
    child.stderr?.on("data", (chunk) => onOutput(String(chunk)));
    child.on("error", (error) => {
      onOutput(`Error: ${error.message}\n`);
      finish({ code: null, timedOut });
    });
    child.on("exit", (code) => finish({ code, timedOut }));
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
  const onOutput = deps.onOutput || (() => {});
  // Resolve uv against the env we'll actually use (PATH may have been augmented
  // with uv's install dirs after a fresh install — the app's own PATH won't
  // pick those up mid-run).
  const env = deps.env ?? process.env;
  const hasUv = deps.hasUv || ((e) => commandExists("uv", e));
  if (hasUv(env)) return { ok: true, env };
  onOutput("Installing uv (required by the Omnigent installer)…\n");
  const run = await runStreaming("sh", ["-c", UV_INSTALLER], {
    spawn: deps.spawn,
    onOutput,
    env,
    timeoutMs: 5 * 60 * 1000,
  });
  // uv's installer drops the binary in ~/.local/bin (or ~/.cargo/bin) and wires
  // PATH only for future shells; add those dirs to the env we hand to the
  // re-check and the installer, mirroring scripts/install_oss.sh's recovery.
  const augmented = withUvDirs(env, deps.home ?? os.homedir());
  if (run.code !== 0 || !hasUv(augmented)) {
    return {
      ok: false,
      error:
        "Couldn't install uv, which the Omnigent installer requires. " +
        "Install it from https://docs.astral.sh/uv/getting-started/installation/ and try again.",
    };
  }
  return { ok: true, env: augmented };
}

/**
 * A copy of `env` with uv's well-known install dirs prepended to PATH, so a
 * just-installed uv (and the omnigent binaries it later drops) resolve for the
 * rest of this run even if the app's launch PATH omitted them.
 *
 * @param {NodeJS.ProcessEnv} env
 * @param {string} home
 * @returns {NodeJS.ProcessEnv}
 */
function withUvDirs(env, home) {
  const dirs = [path.join(home, ".local", "bin"), path.join(home, ".cargo", "bin")];
  const current = env.PATH ? env.PATH.split(":") : [];
  const merged = [...dirs.filter((d) => !current.includes(d)), ...current];
  return { ...env, PATH: merged.join(":") };
}

/**
 * True when `name` resolves on PATH, using `env` (so a PATH augmented with uv's
 * install dirs is honored).
 *
 * @param {string} name
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {boolean}
 */
function commandExists(name, env = process.env) {
  try {
    require("node:child_process").execFileSync("/bin/sh", ["-c", `command -v ${name}`], {
      stdio: "ignore",
      env,
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
  // Only ever run our own bundled script: require an absolute path whose
  // basename is exactly INSTALL_SCRIPT_NAME, then rebuild the argument from that
  // constant so no free-form path string reaches the spawn.
  if (!script || !path.isAbsolute(script) || path.basename(script) !== INSTALL_SCRIPT_NAME) {
    return { ok: false, error: "The bundled installer script was not found." };
  }
  const scriptArg = path.join(path.dirname(script), INSTALL_SCRIPT_NAME);
  const uv = await (deps.ensureUv || ensureUv)({ spawn: deps.spawn, onOutput });
  if (!uv.ok) return uv;

  onOutput("Installing the Omnigent CLI…\n");
  const run = await runStreaming("sh", [scriptArg, "--non-interactive"], {
    spawn: deps.spawn,
    onOutput,
    // Run under the (possibly PATH-augmented) env ensureUv resolved, so a
    // freshly-installed uv is found by the installer.
    env: uv.env,
    timeoutMs: deps.timeoutMs,
    killGraceMs: deps.killGraceMs,
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
