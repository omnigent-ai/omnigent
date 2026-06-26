// Tests for the pure helpers in src/omnigent_cli.js, run with `node --test`
// (no extra deps). The spawning functions need a real binary and are covered by
// the manual verification flow; here we test path resolution order, server-URL
// matching, and status parsing — the logic that decides "is this machine
// connected to server X?" and "which omnigent binary do we run?".

const { describe, it, mock, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");

const {
  normalizeServerUrl,
  isLoopbackServer,
  sameLoopbackServer,
  parseLocalServerPidfile,
  resolveCliPath,
  parseJsonLoose,
  matchesServer,
  parseDaemonRecord,
  daemonServerUrl,
  getHostConnectionFast,
} = require("../src/omnigent_cli");

describe("normalizeServerUrl", () => {
  it("strips trailing slashes and trims", () => {
    assert.equal(normalizeServerUrl("https://x.com/"), "https://x.com");
    assert.equal(normalizeServerUrl("  http://localhost:6767//  "), "http://localhost:6767");
    assert.equal(normalizeServerUrl("https://x.com/ml/omnigents"), "https://x.com/ml/omnigents");
  });

  it("returns empty string for non-strings", () => {
    assert.equal(normalizeServerUrl(undefined), "");
    assert.equal(normalizeServerUrl(null), "");
    assert.equal(normalizeServerUrl(42), "");
  });
});

describe("isLoopbackServer", () => {
  it("is true for loopback hosts", () => {
    assert.equal(isLoopbackServer("http://localhost:6767"), true);
    assert.equal(isLoopbackServer("http://127.0.0.1:6767"), true);
    assert.equal(isLoopbackServer("http://[::1]:6767"), true);
  });

  it("is false for remote hosts and junk", () => {
    assert.equal(isLoopbackServer("https://example.databricksapps.com"), false);
    assert.equal(isLoopbackServer("not a url"), false);
  });
});

describe("sameLoopbackServer", () => {
  it("matches loopback hosts on the same port (localhost == 127.0.0.1)", () => {
    assert.equal(sameLoopbackServer("http://127.0.0.1:6767", "http://localhost:6767/"), true);
    assert.equal(sameLoopbackServer("http://localhost:6767", "http://[::1]:6767"), true);
  });

  it("does not match different ports", () => {
    assert.equal(sameLoopbackServer("http://127.0.0.1:6767", "http://localhost:8000"), false);
  });

  it("does not match when either side is remote, or on junk", () => {
    assert.equal(sameLoopbackServer("http://localhost:6767", "https://example.com:6767"), false);
    assert.equal(sameLoopbackServer("not a url", "http://localhost:6767"), false);
  });
});

describe("parseLocalServerPidfile", () => {
  it("parses pid then port", () => {
    assert.deepEqual(parseLocalServerPidfile("12345\n6767\n"), { pid: 12345, port: 6767 });
    assert.deepEqual(parseLocalServerPidfile("42\n8000"), { pid: 42, port: 8000 });
  });

  it("returns null for malformed contents", () => {
    assert.equal(parseLocalServerPidfile("12345"), null); // only one line
    assert.equal(parseLocalServerPidfile("abc\ndef"), null); // non-numeric
    assert.equal(parseLocalServerPidfile(""), null);
    assert.equal(parseLocalServerPidfile(null), null);
  });
});

describe("resolveCliPath", () => {
  it("prefers a usable configured path", () => {
    const got = resolveCliPath("/custom/omnigent", {
      isExecutableFile: (p) => p === "/custom/omnigent",
      whichOmnigent: () => "/usr/bin/omnigent",
      candidatePaths: () => ["/home/me/.local/bin/omnigent"],
    });
    assert.deepEqual(got, { path: "/custom/omnigent", source: "configured" });
  });

  it("falls back to PATH when the configured path is unusable", () => {
    const got = resolveCliPath("/bad/path", {
      isExecutableFile: (p) => p === "/usr/bin/omnigent",
      whichOmnigent: () => "/usr/bin/omnigent",
      candidatePaths: () => ["/home/me/.local/bin/omnigent"],
    });
    assert.deepEqual(got, { path: "/usr/bin/omnigent", source: "path" });
  });

  it("falls back to a candidate when PATH misses (GUI minimal PATH)", () => {
    const got = resolveCliPath(null, {
      isExecutableFile: (p) => p === "/home/me/.local/bin/omnigent",
      whichOmnigent: () => null,
      candidatePaths: () => ["/home/me/.local/bin/omnigent", "/opt/homebrew/bin/omnigent"],
    });
    assert.deepEqual(got, { path: "/home/me/.local/bin/omnigent", source: "candidate" });
  });

  it("returns null when nothing is usable", () => {
    const got = resolveCliPath(null, {
      isExecutableFile: () => false,
      whichOmnigent: () => null,
      candidatePaths: () => ["/a", "/b"],
    });
    assert.equal(got, null);
  });
});

describe("parseJsonLoose", () => {
  it("parses clean JSON", () => {
    assert.deepEqual(parseJsonLoose('{"running": true}'), { running: true });
  });

  it("recovers JSON after a stray warning line", () => {
    assert.deepEqual(parseJsonLoose('WARN: something\n{"running": false}\n'), {
      running: false,
    });
  });

  it("returns null for empty or unparseable output", () => {
    assert.equal(parseJsonLoose(""), null);
    assert.equal(parseJsonLoose("not json"), null);
  });
});

describe("matchesServer", () => {
  it("matches on server_url or target, ignoring trailing slashes", () => {
    assert.equal(matchesServer({ server_url: "https://x.com/" }, "https://x.com"), true);
    assert.equal(matchesServer({ target: "https://x.com" }, "https://x.com/"), true);
  });

  it("matches a local-mode daemon by its resolved_server_url", () => {
    // target "local", server_url null — only resolved_server_url has the URL.
    assert.equal(
      matchesServer(
        { target: "local", server_url: null, resolved_server_url: "http://127.0.0.1:6767" },
        "http://127.0.0.1:6767/",
      ),
      true,
    );
  });

  it("does not match a different server", () => {
    assert.equal(matchesServer({ server_url: "https://y.com" }, "https://x.com"), false);
  });

  it("is false for junk daemons or empty target", () => {
    assert.equal(matchesServer(null, "https://x.com"), false);
    assert.equal(matchesServer({ server_url: "https://x.com" }, ""), false);
  });
});

describe("parseDaemonRecord", () => {
  it("parses a server-mode record, keeping pid/target/urls", () => {
    assert.deepEqual(
      parseDaemonRecord({
        pid: 4242,
        target: "https://x.com",
        mode: "server",
        server_url: "https://x.com",
        host_id: "host_abc",
        log_path: "/tmp/x.log",
      }),
      {
        pid: 4242,
        target: "https://x.com",
        mode: "server",
        server_url: "https://x.com",
        resolved_server_url: null,
        host_id: "host_abc",
        log_path: "/tmp/x.log",
      },
    );
  });

  it("coerces a string pid (registry writes it either way)", () => {
    assert.equal(parseDaemonRecord({ pid: "99", target: "local", mode: "local" }).pid, 99);
  });

  it("rejects malformed records", () => {
    assert.equal(parseDaemonRecord(null), null);
    assert.equal(parseDaemonRecord({ target: "local", mode: "local" }), null); // no pid
    assert.equal(parseDaemonRecord({ pid: 0, target: "local", mode: "local" }), null); // bad pid
    assert.equal(parseDaemonRecord({ pid: 5, target: "", mode: "local" }), null); // empty target
    assert.equal(parseDaemonRecord({ pid: 5, target: "x", mode: "weird" }), null); // bad mode
  });
});

describe("daemonServerUrl", () => {
  it("uses resolved_server_url for a local-mode daemon, stripping trailing slash", () => {
    assert.equal(
      daemonServerUrl({ mode: "local", resolved_server_url: "http://127.0.0.1:6767/" }),
      "http://127.0.0.1:6767",
    );
  });

  it("uses server_url (then target) for a server-mode daemon", () => {
    assert.equal(
      daemonServerUrl({ mode: "server", server_url: "https://x.com/" }),
      "https://x.com",
    );
    assert.equal(
      daemonServerUrl({ mode: "server", server_url: null, target: "https://y.com" }),
      "https://y.com",
    );
  });

  it("is null for a falsy record", () => {
    assert.equal(daemonServerUrl(null), null);
  });
});

describe("getHostConnectionFast — probe destination & token handling (S1)", () => {
  afterEach(() => {
    mock.restoreAll();
    delete process.env.OMNIGENT_REMOTE_AUTH_TOKEN;
  });

  it("probes the window's serverUrl, never a server_url re-derived from the daemon record", async () => {
    // Planted record: `target` matches the loopback server the window is on (so
    // matchesServer hits), but `server_url` points at an attacker host. The
    // probe must go to the window's serverUrl, not to that disk-record URL.
    const record = {
      pid: process.pid, // a live pid → process is "online", so we reach the probe
      target: "http://localhost:6767",
      mode: "server",
      server_url: "https://evil.com",
      host_id: "host_abc",
    };
    mock.method(fs, "readdirSync", () => ["x.json"]);
    mock.method(fs, "readFileSync", () => JSON.stringify(record));

    const calls = [];
    mock.method(globalThis, "fetch", async (target, init) => {
      calls.push({ target, headers: init?.headers ?? {} });
      return { ok: true, json: async () => ({ status: "online" }) };
    });

    const res = await getHostConnectionFast("http://localhost:6767", { timeoutMs: 100 });

    assert.equal(calls.length, 1);
    // Exact-match the full probe URL: it pins the host to the window's
    // serverUrl, proving the probe did NOT go to the record's `server_url`
    // (https://evil.com).
    assert.equal(calls[0].target, "http://localhost:6767/v1/hosts/host_abc");
    assert.equal(res.connected, true);
  });

  it("never attaches OMNIGENT_REMOTE_AUTH_TOKEN to a probe", async () => {
    // The env token is destination-independent, so the desktop no longer reads
    // it. Even on a loopback probe (which would attach any available bearer),
    // the Authorization header stays absent when only the env var is set.
    process.env.OMNIGENT_REMOTE_AUTH_TOKEN = "secret-token";
    const record = {
      pid: process.pid,
      target: "http://localhost:6767",
      mode: "server",
      server_url: "http://localhost:6767",
      host_id: "host_abc",
    };
    mock.method(fs, "readdirSync", () => ["x.json"]);
    mock.method(fs, "readFileSync", () => JSON.stringify(record));

    const calls = [];
    mock.method(globalThis, "fetch", async (target, init) => {
      calls.push({ target, headers: init?.headers ?? {} });
      return { ok: true, json: async () => ({ status: "online" }) };
    });

    await getHostConnectionFast("http://localhost:6767", { timeoutMs: 100 });

    assert.equal(calls.length, 1);
    assert.equal(calls[0].headers.Authorization, undefined);
  });
});
