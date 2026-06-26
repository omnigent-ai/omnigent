// Tests for the pure helpers in src/omnigent_cli.js, run with `node --test`
// (no extra deps). The spawning functions need a real binary and are covered by
// the manual verification flow; here we test path resolution order, server-URL
// matching, and status parsing — the logic that decides "is this machine
// connected to server X?" and "which omnigent binary do we run?".

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  normalizeServerUrl,
  isLoopbackServer,
  sameLoopbackServer,
  parseLocalServerPidfile,
  resolveCliPath,
  parseJsonLoose,
  matchesServer,
  connectionFromStatus,
  parseDaemonRecord,
  daemonServerUrl,
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

describe("connectionFromStatus", () => {
  const onlineDaemon = {
    server_url: "https://x.com",
    process: "online",
    host_status: "online",
    pid: 1234,
    sessions: [{ id: "a" }, { id: "b" }],
  };

  it("reports connected when process and host_status are both online", () => {
    const conn = connectionFromStatus({ daemons: [onlineDaemon] }, "https://x.com/");
    assert.equal(conn.connected, true);
    assert.equal(conn.process, "online");
    assert.equal(conn.hostStatus, "online");
    assert.equal(conn.pid, 1234);
  });

  it("is not connected when the host tunnel is offline though the process lives", () => {
    const conn = connectionFromStatus(
      { daemons: [{ ...onlineDaemon, host_status: "offline" }] },
      "https://x.com",
    );
    assert.equal(conn.connected, false);
    assert.equal(conn.process, "online");
    assert.equal(conn.hostStatus, "offline");
  });

  it("reports offline when no daemon matches the server", () => {
    const conn = connectionFromStatus({ daemons: [onlineDaemon] }, "https://other.com");
    assert.deepEqual(conn, {
      connected: false,
      process: "offline",
      hostStatus: null,
      pid: null,
      error: null,
    });
  });

  it("tolerates a missing/empty daemons array", () => {
    assert.equal(connectionFromStatus(null, "https://x.com").connected, false);
    assert.equal(connectionFromStatus({}, "https://x.com").connected, false);
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
