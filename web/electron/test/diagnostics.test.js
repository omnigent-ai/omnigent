"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createDiagnostics, safeFields, LOG_NAME, MAX_FILE_BYTES } = require("../src/diagnostics");

function isolatedDiagnostics(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "desktop-diagnostics-test-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const logs = createDiagnostics();
  let configured = false;
  logs.initialize({
    setAppLogsPath: () => {
      configured = true;
    },
    getPath: (name) => {
      assert.equal(name, "logs");
      return directory;
    },
  });
  assert.equal(configured, true);
  return { logs, directory };
}

describe("desktop diagnostics", () => {
  it("records only allowlisted fields and never serializes secrets or URL paths", (t) => {
    const { logs, directory } = isolatedDiagnostics(t);
    const secret = "private-ticket-value";
    logs.record("auth_required", {
      origin: `https://example.test/auth/login?ticket=${secret}`,
      phase: "oidc",
      status: 403,
      code: secret,
      requestId: "req-123",
      error: new Error(secret),
      body: { token: secret },
      outcome: secret,
    });
    const text = fs.readFileSync(path.join(directory, LOG_NAME), "utf8");
    assert.ok(!text.includes(secret));
    assert.ok(!text.includes("/auth/login"));
    assert.deepEqual(JSON.parse(text.trim().split("\n").at(-1)), {
      time: JSON.parse(text.trim().split("\n").at(-1)).time,
      event: "auth_required",
      origin: "https://example.test",
      phase: "oidc",
      status: 403,
      requestId: "req-123",
    });
    assert.deepEqual(safeFields({ code: "invalid_grant", status: 200, interactive: true }), {
      code: "invalid_grant",
      status: 200,
      interactive: true,
    });
  });

  it("rotates bounded logs and exports only the desktop diagnostic files in order", (t) => {
    const { logs, directory } = isolatedDiagnostics(t);
    const file = path.join(directory, LOG_NAME);
    fs.writeFileSync(file, "x".repeat(MAX_FILE_BYTES - 10), { mode: 0o600 });
    logs.record("auth_mode", { phase: "oidc" });
    assert.ok(fs.existsSync(`${file}.1`));
    assert.ok(fs.statSync(file).size <= MAX_FILE_BYTES);
    fs.writeFileSync(path.join(directory, "oidc_tokens.json"), "DO-NOT-EXPORT");
    const destination = path.join(directory, "export.jsonl");
    logs.exportTo(destination);
    const text = fs.readFileSync(destination, "utf8");
    assert.ok(text.startsWith("x"));
    assert.match(text, /"event":"auth_mode"/);
    assert.ok(!text.includes("DO-NOT-EXPORT"));
    assert.ok(fs.statSync(destination).size <= 3 * MAX_FILE_BYTES);
    if (process.platform !== "win32") {
      assert.equal(fs.statSync(destination).mode & 0o777, 0o600);
    }

    const files = [file, `${file}.1`, `${file}.2`];
    fs.writeFileSync(files[2], "oldest event\n");
    const before = files.map((source) => fs.readFileSync(source));
    for (const source of files) {
      assert.throws(() => logs.exportTo(source), /outside the desktop diagnostics log files/);
    }
    if (process.platform !== "win32") {
      const alias = path.join(directory, "log-alias");
      fs.symlinkSync(directory, alias, "dir");
      const fileAlias = path.join(directory, "file-alias.jsonl");
      fs.symlinkSync(files[1], fileAlias);
      assert.throws(() => logs.exportTo(fileAlias), /outside the desktop diagnostics log files/);
      assert.throws(
        () => logs.exportTo(path.join(alias, LOG_NAME)),
        /outside the desktop diagnostics log files/,
      );
    }
    for (const [index, source] of files.entries()) {
      assert.deepEqual(fs.readFileSync(source), before[index]);
    }
  });

  it("does not include an oversized or missing backup", (t) => {
    const { logs, directory } = isolatedDiagnostics(t);
    fs.writeFileSync(path.join(directory, `${LOG_NAME}.1`), "s".repeat(MAX_FILE_BYTES + 1));
    assert.ok(!logs.snapshot().includes("s".repeat(100)));
  });
});
