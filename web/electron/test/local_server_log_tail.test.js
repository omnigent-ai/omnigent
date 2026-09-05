// Tests for the setup-terminal log tail helpers (src/omnigent_cli.js), run with
// `node --test`. Focus: the line splitter's partial-line buffering across chunk
// boundaries (the classic tail bug — never emit a half line), CRLF trimming,
// ANSI stripping, live-log discovery (freshest file, stale rejected), and the
// tail streaming during boot then stopping on abort.

const { describe, it, before, after, beforeEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  makeLineSplitter,
  stripAnsi,
  findLiveServerLog,
  tailLocalServerLog,
} = require("../src/omnigent_cli");

describe("makeLineSplitter", () => {
  it("emits whole lines and buffers a trailing partial across chunks", () => {
    const out = [];
    const s = makeLineSplitter((line) => out.push(line));
    s.push("hello\nwor");
    assert.deepEqual(out, ["hello"]); // "wor" is buffered, not emitted
    s.push("ld\n");
    assert.deepEqual(out, ["hello", "world"]);
  });

  it("splits a line that arrives one byte at a time", () => {
    const out = [];
    const s = makeLineSplitter((line) => out.push(line));
    for (const ch of "ab\ncd\n") s.push(ch);
    assert.deepEqual(out, ["ab", "cd"]);
  });

  it("handles multiple newlines in one chunk", () => {
    const out = [];
    const s = makeLineSplitter((line) => out.push(line));
    s.push("a\nb\nc\n");
    assert.deepEqual(out, ["a", "b", "c"]);
  });

  it("trims a trailing CR (CRLF logs)", () => {
    const out = [];
    const s = makeLineSplitter((line) => out.push(line));
    s.push("line\r\nnext\r\n");
    assert.deepEqual(out, ["line", "next"]);
  });

  it("does not emit a final unterminated line", () => {
    const out = [];
    const s = makeLineSplitter((line) => out.push(line));
    s.push("no newline here");
    assert.deepEqual(out, []);
  });
});

describe("stripAnsi", () => {
  it("removes SGR color codes, keeps text", () => {
    assert.equal(stripAnsi("\x1b[32mINFO\x1b[0m ready"), "INFO ready");
  });
  it("leaves plain text untouched", () => {
    assert.equal(stripAnsi("plain line"), "plain line");
  });
});

// findLiveServerLog + tailLocalServerLog read from <OMNIGENT_DATA_DIR>/logs/server,
// so point the env var at a temp dir and lay out server-*.log files.
describe("findLiveServerLog", () => {
  let dataDir;
  let logDir;
  const prevEnv = process.env.OMNIGENT_DATA_DIR;

  before(() => {
    dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-logtail-"));
    process.env.OMNIGENT_DATA_DIR = dataDir;
    logDir = path.join(dataDir, "logs", "server");
    fs.mkdirSync(logDir, { recursive: true });
  });
  after(() => {
    if (prevEnv === undefined) delete process.env.OMNIGENT_DATA_DIR;
    else process.env.OMNIGENT_DATA_DIR = prevEnv;
    fs.rmSync(dataDir, { recursive: true, force: true });
  });
  beforeEach(() => {
    for (const f of fs.readdirSync(logDir)) fs.rmSync(path.join(logDir, f));
  });

  const write = (name, mtimeMs) => {
    const full = path.join(logDir, name);
    fs.writeFileSync(full, "");
    const s = mtimeMs / 1000;
    fs.utimesSync(full, s, s);
    return full;
  };

  it("returns null when the log dir has no matching file", () => {
    assert.equal(findLiveServerLog(Date.now()), null);
  });

  it("picks the newest server-*.log at/after the start floor", () => {
    const now = Date.now();
    write("server-old.log", now + 1000);
    const newer = write("server-new.log", now + 5000);
    assert.equal(findLiveServerLog(now), newer);
  });

  it("rejects a stale log written before the start floor (prior crash)", () => {
    const now = Date.now();
    // Well before the floor (which is startedAt - 2000ms tolerance).
    write("server-stale.log", now - 60_000);
    assert.equal(findLiveServerLog(now), null);
  });

  it("ignores non-server files in the dir", () => {
    const now = Date.now();
    write("runner-x.log", now + 5000);
    write("notes.txt", now + 5000);
    const s = write("server-y.log", now + 1000);
    assert.equal(findLiveServerLog(now), s);
  });
});

describe("tailLocalServerLog", () => {
  let dataDir;
  let logDir;
  const prevEnv = process.env.OMNIGENT_DATA_DIR;

  before(() => {
    dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-logtail-run-"));
    process.env.OMNIGENT_DATA_DIR = dataDir;
    logDir = path.join(dataDir, "logs", "server");
    fs.mkdirSync(logDir, { recursive: true });
  });
  after(() => {
    if (prevEnv === undefined) delete process.env.OMNIGENT_DATA_DIR;
    else process.env.OMNIGENT_DATA_DIR = prevEnv;
    fs.rmSync(dataDir, { recursive: true, force: true });
  });
  beforeEach(() => {
    for (const f of fs.readdirSync(logDir)) fs.rmSync(path.join(logDir, f));
  });

  it("streams lines appended DURING boot, then stops on abort", async () => {
    const now = Date.now();
    const logFile = path.join(logDir, "server-boot.log");
    fs.writeFileSync(logFile, "INFO starting\n");
    fs.utimesSync(logFile, (now + 100) / 1000, (now + 100) / 1000);

    const lines = [];
    const controller = new AbortController();
    const done = tailLocalServerLog((l) => lines.push(l), {
      signal: controller.signal,
      startedAtMs: now,
      pollMs: 10,
    });

    // Append more lines after the tail has started — these must stream, not be
    // dumped only at the end (the bug: sidecar discovery streamed nothing live).
    await new Promise((r) => {
      setTimeout(r, 40);
    });
    fs.appendFileSync(logFile, "INFO migrating\nINFO Uvicorn running\n");
    await new Promise((r) => {
      setTimeout(r, 40);
    });

    controller.abort();
    await done;

    assert.deepEqual(lines, ["INFO starting", "INFO migrating", "INFO Uvicorn running"]);
  });

  it("returns promptly on abort when no logfile ever appears (failed start)", async () => {
    const controller = new AbortController();
    const started = Date.now();
    const done = tailLocalServerLog(() => {}, {
      signal: controller.signal,
      startedAtMs: started,
      pollMs: 10,
      // A large discover timeout would hang if abort weren't honored.
      discoverTimeoutMs: 60_000,
    });
    controller.abort();
    await done;
    // Aborting during discovery must not wait out discoverTimeoutMs.
    assert.ok(Date.now() - started < 1000);
  });
});
