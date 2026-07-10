// Tests for src/loginShellPath.js — the login-shell PATH resolver that patches
// process.env.PATH for GUI-launched Electron (see #1933). Run with `node --test`
// (no extra deps). These exercise the REAL module: resolveLoginShellPath takes
// execFileSync/os/env/platform as injectable deps, so we drive every outcome with
// mocks and never spawn a shell. A source-guard at the end pins the main.js wiring
// so it can't silently regress to a bare PATH replace.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");

const {
  resolveLoginShellPath,
  mergePath,
  extractPath,
  isLikelyLoginPath,
  stripAnsi,
} = require("../src/loginShellPath");

const START = "__OMNIGENT_PATH_START__";
const END = "__OMNIGENT_PATH_END__";
const ESC = String.fromCharCode(27);

// A stripped launchd-style PATH (the input state this whole module exists to fix).
const STRIPPED = "/usr/bin:/bin:/usr/sbin:/sbin";
const zshUser = { userInfo: () => ({ shell: "/bin/zsh" }) };

describe("extractPath", () => {
  it("pulls the PATH out from between the delimiters", () => {
    const out = `${START}/opt/homebrew/bin:/usr/bin${END}`;
    assert.equal(extractPath(out), "/opt/homebrew/bin:/usr/bin");
  });

  it("ignores an rc-file banner printed around the delimited value", () => {
    const out = `Last login: whenever\nnvm loaded\n${START}/opt/homebrew/bin:/usr/bin${END}\n`;
    assert.equal(extractPath(out), "/opt/homebrew/bin:/usr/bin");
  });

  it("strips ANSI color codes inside the delimited value", () => {
    const out = `${START}${ESC}[32m/opt/homebrew/bin:/usr/bin${ESC}[0m${END}`;
    assert.equal(extractPath(out), "/opt/homebrew/bin:/usr/bin");
  });

  it("returns null when markers are absent or non-string", () => {
    assert.equal(extractPath("no markers here"), null);
    assert.equal(extractPath(`${START}only start marker`), null);
    assert.equal(extractPath(undefined), null);
    assert.equal(extractPath(`${START}${END}`), null); // empty
  });
});

describe("stripAnsi", () => {
  it("removes SGR escape sequences", () => {
    assert.equal(stripAnsi(`${ESC}[1;32mgreen${ESC}[0m`), "green");
  });
  it("leaves plain text untouched", () => {
    assert.equal(stripAnsi("/opt/homebrew/bin:/usr/bin"), "/opt/homebrew/bin:/usr/bin");
  });
});

describe("isLikelyLoginPath (fast-path skip)", () => {
  it("is true on darwin when a Homebrew dir is already present", () => {
    assert.equal(isLikelyLoginPath({ PATH: "/opt/homebrew/bin:/usr/bin" }, "darwin"), true);
    assert.equal(isLikelyLoginPath({ PATH: "/usr/local/bin:/usr/bin" }, "darwin"), true);
  });
  it("is false on darwin for a stripped PATH", () => {
    assert.equal(isLikelyLoginPath({ PATH: STRIPPED }, "darwin"), false);
  });
  it("is false off darwin (never skip on Linux)", () => {
    assert.equal(isLikelyLoginPath({ PATH: "/opt/homebrew/bin" }, "linux"), false);
  });
});

describe("resolveLoginShellPath", () => {
  it("resolves via an interactive+login shell using the passwd-DB shell", () => {
    const calls = [];
    const execFileSync = (shell, args) => {
      calls.push({ shell, args });
      return `${START}/opt/homebrew/bin:/usr/bin${END}`;
    };
    const result = resolveLoginShellPath({
      execFileSync,
      os: zshUser,
      env: { PATH: STRIPPED }, // $SHELL intentionally absent (GUI launch)
      platform: "darwin",
    });
    assert.equal(result, "/opt/homebrew/bin:/usr/bin");
    // Uses os.userInfo().shell, not $SHELL, and runs interactive+login.
    assert.equal(calls[0].shell, "/bin/zsh");
    assert.equal(calls[0].args[0], "-ilc");
  });

  it("prefers $SHELL when the passwd DB has no shell", () => {
    const calls = [];
    const execFileSync = (shell) => {
      calls.push(shell);
      return `${START}/x${END}`;
    };
    resolveLoginShellPath({
      execFileSync,
      os: { userInfo: () => ({}) },
      env: { PATH: STRIPPED, SHELL: "/usr/bin/fish" },
      platform: "darwin",
    });
    assert.equal(calls[0], "/usr/bin/fish");
  });

  it("returns null on win32 without spawning anything", () => {
    let spawned = false;
    const result = resolveLoginShellPath({
      execFileSync: () => {
        spawned = true;
        return "";
      },
      os: zshUser,
      env: { PATH: "C:\\Windows" },
      platform: "win32",
    });
    assert.equal(result, null);
    assert.equal(spawned, false);
  });

  it("skips the spawn when PATH already looks complete (fast path)", () => {
    let spawned = false;
    const result = resolveLoginShellPath({
      execFileSync: () => {
        spawned = true;
        return "";
      },
      os: zshUser,
      env: { PATH: "/opt/homebrew/bin:/usr/bin" },
      platform: "darwin",
    });
    assert.equal(result, null);
    assert.equal(spawned, false);
  });

  it("falls through to the next shell when the first fails, and returns null if all fail", () => {
    const tried = [];
    const execFileSync = (shell) => {
      tried.push(shell);
      throw new Error(`cannot spawn ${shell}`);
    };
    const result = resolveLoginShellPath({
      execFileSync,
      os: zshUser,
      env: { PATH: STRIPPED },
      platform: "darwin",
    });
    assert.equal(result, null);
    // Tried the user shell plus POSIX fallbacks (deduped).
    assert.ok(tried.length >= 2);
    assert.ok(tried.includes("/bin/bash"));
  });

  it("recovers a delimited PATH from err.stdout on non-zero exit", () => {
    const execFileSync = () => {
      const err = new Error("shell exited 1");
      err.stdout = `warning: something\n${START}/opt/homebrew/bin${END}`;
      throw err;
    };
    const result = resolveLoginShellPath({
      execFileSync,
      os: zshUser,
      env: { PATH: STRIPPED },
      platform: "darwin",
    });
    assert.equal(result, "/opt/homebrew/bin");
  });
});

describe("mergePath", () => {
  it("unions the two, login PATH first, de-duplicating", () => {
    assert.equal(
      mergePath("/usr/bin:/x", "/opt/homebrew/bin:/usr/bin"),
      "/opt/homebrew/bin:/usr/bin:/x",
    );
  });
  it("preserves a current-only dir the login shell lacks (real merge, not replace)", () => {
    assert.equal(mergePath("/app/injected:/usr/bin", "/usr/bin"), "/usr/bin:/app/injected");
  });
  it("tolerates empty inputs", () => {
    assert.equal(mergePath("", "/usr/bin"), "/usr/bin");
    assert.equal(mergePath("/usr/bin", ""), "/usr/bin");
  });
});

// Source-guard: main.js must merge (not replace) the resolved PATH. A behavior
// test can't see main.js wiring, and a bare `process.env.PATH = _loginPath` would
// silently drop app-injected dirs — so pin the call shape here.
describe("main.js wiring", () => {
  const mainSource = readFileSync(path.join(__dirname, "../src/main.js"), "utf8");
  const liveCode = mainSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

  it("merges the login PATH rather than replacing it", () => {
    assert.match(liveCode, /process\.env\.PATH\s*=\s*mergePath\(/);
    assert.doesNotMatch(liveCode, /process\.env\.PATH\s*=\s*_loginPath\b/);
  });
});
