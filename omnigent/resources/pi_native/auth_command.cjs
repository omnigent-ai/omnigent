// Pi discards helper stderr; retain only a classified, token-free result.
const fs = require("fs");
const { spawn } = require("child_process");

const configPath = process.argv[2];
const refreshing = process.argv[3] === "--refresh";
let config;
if (!refreshing) {
  try {
    fs.unlinkSync(`${configPath}.result.json`);
  } catch (_) {
    // A previous attempt may have no result.
  }
}
const started = performance.now();
let deadline = Infinity;
let child;
let timer;
let finished = false;
let chunks = [];
let bytes = 0;

function killHelper() {
  if (!child || !child.pid) return;
  try {
    process.kill(-child.pid, "SIGKILL");
  } catch (_) {
    // The group may already have exited.
  }
}

function finish(kind, token) {
  if (finished) return;
  finished = true;
  clearTimeout(timer);
  killHelper();
  chunks = [];
  try {
    if (kind === "ok" && refreshing && config.refreshIntervalMs > 0) {
      const cachedAt = Date.now();
      let expiresAt = cachedAt + config.refreshIntervalMs;
      // JWT expiry can shorten the configured cache lifetime, never extend it.
      if (token.split(".").length === 3) {
        try {
          const payload = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString());
          if (typeof payload.exp === "number") expiresAt = Math.min(expiresAt, payload.exp * 1000);
        } catch (_) {
          expiresAt = 0;
        }
      }
      const temp = `${config.cachePath}.${process.pid}.tmp`;
      fs.writeFileSync(temp, JSON.stringify({ token, cachedAt, expiresAt }), { mode: 0o600 });
      fs.renameSync(temp, config.cachePath);
    }
    if (!refreshing) {
      const result = {
        kind,
        piPid: Number(process.env.OMNIGENT_PI_NATIVE_PID),
        at: Date.now(),
        elapsedMs: Math.round(performance.now() - started),
      };
      const temp = `${configPath}.${process.pid}.tmp`;
      fs.writeFileSync(temp, JSON.stringify(result), { mode: 0o600 });
      fs.renameSync(temp, `${configPath}.result.json`);
    }
  } catch (_) {
    // A failed private-file write must never publish a token.
    process.exit(1);
  }
  if (kind === "ok") {
    process.stdout.write(token, () => process.exit(0));
  } else {
    process.exit(1);
  }
}

const cancel = () => finish(performance.now() >= deadline ? "timeout" : "cancelled");
process.on("SIGTERM", cancel);
process.on("SIGINT", cancel);
process.on("exit", () => {
  if (!finished) killHelper();
});

try {
  config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  if (typeof config.command !== "string" || !config.command.trim()) {
    return finish("invalid_command");
  }
  if (!refreshing && config.refreshIntervalMs > 0) {
    try {
      const cached = JSON.parse(fs.readFileSync(config.cachePath, "utf8"));
      if (cached.expiresAt > Date.now() && typeof cached.token === "string" && cached.token.trim()) {
        return finish("ok", cached.token);
      }
    } catch (_) {
      // An absent or invalid cache uses the bounded request-time helper.
    }
  }
  deadline = started + (refreshing ? config.refreshTimeoutMs : config.timeoutMs);
  timer = setTimeout(() => finish("timeout"), Math.max(0, deadline - performance.now()));
  child = spawn(config.command, {
    shell: true,
    detached: true,
    stdio: ["ignore", "pipe", "ignore"],
  });
  child.on("error", () => finish("start_failed"));
  child.stdout.on("data", (chunk) => {
    bytes += chunk.length;
    if (bytes > 1024 * 1024) finish("output_limit");
    else chunks.push(chunk);
  });
  child.on("close", (code) => {
    if (finished) return;
    if (code !== 0) return finish("failed_exit");
    try {
      const token = new TextDecoder("utf-8", { fatal: true })
        .decode(Buffer.concat(chunks))
        .trim();
      finish(token ? "ok" : "empty", token);
    } catch (_) {
      finish("invalid_output");
    }
  });
} catch (_) {
  finish("start_failed");
}
