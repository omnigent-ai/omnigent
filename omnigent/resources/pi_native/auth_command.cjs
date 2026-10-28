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
let retryTimer;
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
  clearTimeout(retryTimer);
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

function retryOrFinish(kind, command, retryDelays) {
  const [delay, ...rest] = retryDelays;
  if (delay === undefined || performance.now() + delay >= deadline) {
    return finish(kind);
  }
  killHelper();
  child = undefined;
  retryTimer = setTimeout(() => {
    // A stalled event loop can run this after the deadline; the deadline timer finishes then.
    if (performance.now() >= deadline) return;
    try {
      runAttempt(command, rest);
    } catch (_) {
      finish("start_failed");
    }
  }, delay);
}

function runAttempt(command, retryDelays) {
  chunks = [];
  bytes = 0;
  const attempt = spawn(command, {
    shell: true,
    detached: true,
    stdio: ["ignore", "pipe", "ignore"],
  });
  child = attempt;
  attempt.on("error", () => {
    if (attempt === child) finish("start_failed");
  });
  attempt.stdout.on("data", (chunk) => {
    if (attempt !== child) return;
    bytes += chunk.length;
    if (bytes > 1024 * 1024) finish("output_limit");
    else chunks.push(chunk);
  });
  attempt.on("close", (code) => {
    if (finished || attempt !== child) return;
    if (code !== 0) return retryOrFinish("failed_exit", command, retryDelays);
    let token;
    try {
      token = new TextDecoder("utf-8", { fatal: true })
        .decode(Buffer.concat(chunks))
        .trim();
    } catch (_) {
      return finish("invalid_output");
    }
    if (token) finish("ok", token);
    else retryOrFinish("empty", command, retryDelays);
  });
}

try {
  const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  if (typeof config.command !== "string" || !config.command.trim()) {
    return finish("invalid_command");
  }
  deadline = started + config.timeoutMs;
  timer = setTimeout(() => finish("timeout"), Math.max(0, deadline - performance.now()));
  runAttempt(config.command, config.retryDelaysMs || []);
} catch (_) {
  finish("start_failed");
}
