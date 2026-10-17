// Pi discards helper stderr; retain only a classified, token-free result.
const fs = require("fs");
const { spawn } = require("child_process");

const configPath = process.argv[2];
try {
  fs.unlinkSync(`${configPath}.result.json`);
} catch (_) {
  // A previous attempt may have no result.
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
    const result = {
      kind,
      piPid: Number(process.env.OMNIGENT_PI_NATIVE_PID),
      at: Date.now(),
      elapsedMs: Math.round(performance.now() - started),
    };
    const temp = `${configPath}.${process.pid}.tmp`;
    fs.writeFileSync(temp, JSON.stringify(result), { mode: 0o600 });
    fs.renameSync(temp, `${configPath}.result.json`);
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
  const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  if (typeof config.command !== "string" || !config.command.trim()) {
    return finish("invalid_command");
  }
  deadline = started + config.timeoutMs;
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
