// A real `omnigent host` daemon plus a host-launched session for desktop e2e.
//
// Mirrors tests/_helpers/host.py: owned HOME/config/data, a unique host
// identity, and readiness read from the server's own /v1/hosts listing. The
// session is created through the product API exactly as the SPA does for a
// host (JSON create with `host_id` + `workspace`), so the server binds the
// host and the host launches the runner.

"use strict";

const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const REPO_ROOT = path.resolve(__dirname, "..", "..", "..", "..");
const PYTHON = process.env.OMNIGENT_PYTHON || "python3";

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function fetchJson(url, init) {
  const res = await fetch(url, init);
  const text = await res.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    body = text;
  }
  return { status: res.status, body };
}

/** Isolated process env for a host: no inherited identity, proxies or provider auth. */
function hostEnv(home, extra) {
  // Mirrors tests/_helpers/live_server.py: drop ambient Omnigent/runner identity, proxy
  // settings and provider/auth secrets, keep loopback direct, then apply explicit inputs.
  const inherited = Object.entries(process.env).filter(
    ([key]) =>
      !key.startsWith("OMNIGENT") &&
      !key.startsWith("DATABRICKS_") &&
      !key.endsWith("_SECRET") &&
      !/^(https?_proxy|all_proxy|no_proxy|RUNNER_SERVER_URL|ANTHROPIC_API_KEY|OPENAI_API_KEY|OPENAI_BASE_URL|CODEX_API_KEY)$/i.test(
        key,
      ),
  );
  return {
    ...Object.fromEntries(inherited),
    HOME: home,
    PYTHONPATH: REPO_ROOT,
    NO_PROXY: "127.0.0.1,localhost",
    no_proxy: "127.0.0.1,localhost",
    OMNIGENT_DATA_DIR: path.join(home, ".omnigent"),
    OMNIGENT_CONFIG_HOME: path.join(home, ".omnigent"),
    OMNIGENT_AUTH_PROVIDER: "header",
    OMNIGENT_LOCAL_SINGLE_USER: "1",
    OMNIGENT_DISABLE_CATALOG_LOOKUP: "1",
    OMNIGENT_DISABLE_TELEMETRY: "1",
    ...extra,
  };
}

/**
 * Spawn a host daemon against `serverUrl` and wait until the server lists it online.
 *
 * @param {string} serverUrl Backend the host tunnels to.
 * @param {string} root Fresh scratch dir for HOME, config and the log.
 * @param {{ env?: Record<string, string>, timeoutMs?: number }} [options]
 * @returns {Promise<{ hostId: string, logPath: string, close: () => Promise<void> }>}
 */
async function spawnHost(serverUrl, root, { env: extra = {}, timeoutMs = 60_000 } = {}) {
  const home = path.join(root, "host-home");
  const config = path.join(home, ".omnigent");
  fs.mkdirSync(config, { recursive: true });
  const hostId = crypto.randomUUID().replaceAll("-", "");
  const name = `e2e-host-${hostId.slice(0, 12)}`;
  fs.writeFileSync(
    path.join(config, "config.yaml"),
    `host:\n  host_id: ${hostId}\n  name: ${name}\n`,
  );
  const logPath = path.join(root, "host.log");
  fs.writeFileSync(logPath, "");
  const log = fs.openSync(logPath, "a");
  const proc = spawn(PYTHON, ["-m", "omnigent.host._daemon_entry", "--server", serverUrl], {
    env: hostEnv(home, {
      OMNIGENT_HOST_ID: hostId,
      OMNIGENT_HOST_NAME: name,
      OMNIGENT_PROCESS_LOG_FILE: logPath,
      ...extra,
    }),
    stdio: ["ignore", log, log],
  });
  const close = async () => {
    if (proc.exitCode === null) proc.kill("SIGTERM");
    await sleep(300);
    if (proc.exitCode === null) proc.kill("SIGKILL");
    try {
      fs.closeSync(log);
    } catch {
      /* already closed */
    }
  };
  const deadline = Date.now() + timeoutMs;
  let last = "not polled";
  // oxlint-disable no-await-in-loop -- readiness polling.
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) break;
    try {
      const { status, body } = await fetchJson(`${serverUrl}/v1/hosts`);
      last = `HTTP ${status}`;
      const row = (body?.hosts ?? []).find((h) => h.host_id === hostId);
      if (row?.status === "online") return { hostId, logPath, close };
      if (row) last = `${last} status=${row.status}`;
    } catch (err) {
      last = String(err);
    }
    await sleep(500);
  }
  // oxlint-enable no-await-in-loop
  await close();
  throw new Error(
    `host ${hostId} never came online (${last}; exit=${proc.exitCode}).\n` +
      fs.readFileSync(logPath, "utf8").slice(-4000),
  );
}

/**
 * Create a session on `hostId` with `workspace` and wait until its runner
 * serves the workspace listing.
 *
 * @returns {Promise<{ sessionId: string, runnerId: string | null }>}
 */
async function createHostSession(
  serverUrl,
  { hostId, workspace, agentName = "hello_world", timeoutMs = 90_000 },
) {
  const agents = await fetchJson(`${serverUrl}/v1/agents`);
  const rows = Array.isArray(agents.body)
    ? agents.body
    : (agents.body?.data ?? agents.body?.agents);
  const agent = (rows ?? []).find((a) => a.name === agentName);
  if (!agent) {
    throw new Error(`agent ${agentName} not listed by /v1/agents: ${JSON.stringify(agents.body)}`);
  }
  const created = await fetchJson(`${serverUrl}/v1/sessions`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ agent_id: agent.id, host_id: hostId, workspace }),
  });
  if (created.status !== 200 && created.status !== 201) {
    throw new Error(`session create failed: ${created.status} ${JSON.stringify(created.body)}`);
  }
  const sessionId = created.body.id ?? created.body.session_id;
  const listing = `${serverUrl}/v1/sessions/${sessionId}/resources/environments/default/filesystem`;
  const deadline = Date.now() + timeoutMs;
  let last = "not polled";
  // oxlint-disable no-await-in-loop -- readiness polling.
  while (Date.now() < deadline) {
    const { status, body } = await fetchJson(listing);
    last = `HTTP ${status} ${typeof body === "string" ? body.slice(0, 200) : JSON.stringify(body).slice(0, 200)}`;
    if (status === 200) {
      return { sessionId, runnerId: created.body.runner_id ?? null, listing: body };
    }
    await sleep(1000);
  }
  // oxlint-enable no-await-in-loop
  throw new Error(`session ${sessionId} workspace never became listable (${last})`);
}

module.exports = { spawnHost, createHostSession, REPO_ROOT };
