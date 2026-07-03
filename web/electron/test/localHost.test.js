// Tests for the local-host spawn/status logic (src/localHost.js), run with
// `node --test` (no extra deps). Covers the pure, dependency-injected pieces:
// the connection-mode decision, the `omni host` argv it builds, the daemon
// registry path, needsLogin from an auth-tokens fixture, and runtime
// resolution under the Finder minimal-PATH constraint. The effectful spawn
// (detached child) is exercised by manual e2e, not here.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");

const {
  decideMode,
  isLoopbackServer,
  daemonArgs,
  daemonTarget,
  daemonRecordPath,
  computeNeedsLogin,
  resolveOmnigentRuntime,
  readDaemonRecord,
} = require("../src/localHost");

describe("decideMode", () => {
  it("is local ONLY with no server URL — the from-scratch spawn case", () => {
    assert.equal(decideMode(null), "local");
    assert.equal(decideMode(""), "local");
  });

  it("connects to a concrete server — loopback included — with --server", () => {
    // A loopback URL means the UI already started/pinned a server; the host
    // must dial THAT one, not spawn a competing second server.
    assert.equal(decideMode("http://localhost:6767"), "remote");
    assert.equal(decideMode("http://127.0.0.1:6767"), "remote");
    assert.equal(decideMode("http://[::1]:6767/"), "remote");
    assert.equal(decideMode("https://omnigent-app.databricksapps.com"), "remote");
    assert.equal(decideMode("https://example.com:8443/path"), "remote");
  });

  it("falls back to local on an unparseable URL", () => {
    assert.equal(decideMode("not a url"), "local");
  });
});

describe("isLoopbackServer", () => {
  it("is true only for a parseable loopback URL", () => {
    assert.equal(isLoopbackServer("http://localhost:6767"), true);
    assert.equal(isLoopbackServer("http://127.0.0.1:6767"), true);
    assert.equal(isLoopbackServer("http://[::1]:6767/"), true);
    assert.equal(isLoopbackServer("https://example.com"), false);
    assert.equal(isLoopbackServer(null), false);
    assert.equal(isLoopbackServer(""), false);
    assert.equal(isLoopbackServer("not a url"), false);
  });
});

describe("daemonArgs", () => {
  it('uses the empty-string positional for local mode (omni host "")', () => {
    assert.deepEqual(daemonArgs("local", null), ["host", ""]);
  });

  it("passes --server for remote mode — including a loopback server", () => {
    assert.deepEqual(daemonArgs("remote", "https://example.com"), [
      "host",
      "--server",
      "https://example.com",
    ]);
    assert.deepEqual(daemonArgs("remote", "http://127.0.0.1:6767"), [
      "host",
      "--server",
      "http://127.0.0.1:6767",
    ]);
  });
});

describe("daemonTarget / daemonRecordPath", () => {
  it("normalizes the target the same way the CLI keys its registry", () => {
    assert.equal(daemonTarget("local", null), "local");
    assert.equal(daemonTarget("remote", "https://example.com/"), "https://example.com");
  });

  it("hashes the target to the CLI's sha256[:16].json record path", () => {
    const target = "https://example.com";
    const digest = crypto.createHash("sha256").update(target, "utf8").digest("hex").slice(0, 16);
    assert.ok(daemonRecordPath(target).endsWith(`${digest}.json`));
  });
});

describe("computeNeedsLogin", () => {
  const future = () => 1_000_000; // ms "now"
  const tokenFile = (obj) => ({
    readFileSync: () => JSON.stringify(obj),
    now: future,
  });

  it("is false for a local server regardless of stored tokens", () => {
    assert.equal(computeNeedsLogin(null, tokenFile({})), false);
    assert.equal(computeNeedsLogin("http://localhost:6767", tokenFile({})), false);
  });

  it("is true for a remote server with no token file", () => {
    assert.equal(
      computeNeedsLogin("https://example.com", {
        readFileSync: () => {
          throw new Error("ENOENT");
        },
        now: future,
      }),
      true,
    );
  });

  it("is false when a valid (unexpired) token is stored", () => {
    assert.equal(
      computeNeedsLogin(
        "https://example.com",
        tokenFile({ "https://example.com": { token: "jwt", expires_at: 100_000 } }),
      ),
      false,
    );
  });

  it("treats an expired token as missing", () => {
    // expires_at is in seconds; 1 * 1000 < 1_000_000 → expired.
    assert.equal(
      computeNeedsLogin(
        "https://example.com",
        tokenFile({ "https://example.com": { token: "jwt", expires_at: 1 } }),
      ),
      true,
    );
  });

  it("treats a Databricks pointer record as having credentials", () => {
    assert.equal(
      computeNeedsLogin(
        "https://example.com",
        tokenFile({ "https://example.com": { auth_type: "databricks", workspace_host: "x" } }),
      ),
      false,
    );
  });

  it("matches the key with trailing slashes stripped", () => {
    assert.equal(
      computeNeedsLogin(
        "https://example.com/",
        tokenFile({ "https://example.com": { token: "jwt", expires_at: 100_000 } }),
      ),
      false,
    );
  });
});

describe("resolveOmnigentRuntime", () => {
  it("prefers an executable in a known bin dir", () => {
    const result = resolveOmnigentRuntime({
      noCache: true,
      binDirs: ["/opt/homebrew/bin", "/usr/local/bin"],
      accessSync: (p) => {
        if (p !== "/opt/homebrew/bin/omnigent") throw new Error("ENOENT");
      },
      runShellProbe: () => null,
      pathEnv: "",
    });
    assert.deepEqual(result, { binary: "/opt/homebrew/bin/omnigent", binDir: "/opt/homebrew/bin" });
  });

  it("falls back to the login-shell probe when known dirs miss (Finder PATH)", () => {
    const result = resolveOmnigentRuntime({
      noCache: true,
      binDirs: ["/opt/homebrew/bin"],
      accessSync: (p) => {
        // Known dir misses; only the probe's path is executable.
        if (p !== "/Users/me/.local/bin/omni") throw new Error("ENOENT");
      },
      runShellProbe: () => "/Users/me/.local/bin/omni",
      pathEnv: "",
    });
    assert.deepEqual(result, {
      binary: "/Users/me/.local/bin/omni",
      binDir: "/Users/me/.local/bin",
    });
  });

  it("falls back to the process PATH last", () => {
    const result = resolveOmnigentRuntime({
      noCache: true,
      binDirs: [],
      accessSync: (p) => {
        if (p !== "/custom/bin/omnigent") throw new Error("ENOENT");
      },
      runShellProbe: () => null,
      pathEnv: "/custom/bin:/other",
    });
    assert.deepEqual(result, { binary: "/custom/bin/omnigent", binDir: "/custom/bin" });
  });

  it("returns null when nothing resolves", () => {
    const result = resolveOmnigentRuntime({
      noCache: true,
      binDirs: ["/opt/homebrew/bin"],
      accessSync: () => {
        throw new Error("ENOENT");
      },
      runShellProbe: () => null,
      pathEnv: "",
    });
    assert.equal(result, null);
  });
});

describe("readDaemonRecord", () => {
  it("reports not-running when no record exists", () => {
    const record = readDaemonRecord("local", {
      readFileSync: () => {
        throw new Error("ENOENT");
      },
    });
    assert.deepEqual(record, { running: false, pid: null, logPath: null });
  });

  it("reports not-running for a dead pid but surfaces the log path", () => {
    const record = readDaemonRecord("local", {
      // pid 1 is almost certainly not us and kill(1,0) raises EPERM... so use a
      // pid that is structurally valid but (very likely) dead.
      readFileSync: () => JSON.stringify({ pid: 2 ** 31 - 1, log_path: "/tmp/d.log" }),
    });
    assert.equal(record.running, false);
    assert.equal(record.logPath, "/tmp/d.log");
  });
});
