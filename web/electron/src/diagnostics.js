"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");

const LOG_NAME = "desktop-diagnostics.jsonl";
const MAX_FILE_BYTES = 1024 * 1024;
const BACKUPS = 2;
const ALLOWED_FIELDS = new Set([
  "origin",
  "phase",
  "outcome",
  "code",
  "status",
  "requestId",
  "interactive",
]);
const SAFE_CODES = new Set([
  "AbortError",
  "BROWSER_UNAVAILABLE",
  "LOOPBACK_TIMEOUT",
  "NO_STORED_TOKEN",
  "NO_REFRESH_TOKEN",
  "SESSION_REJECTED",
  "SESSION_TRANSPORT",
  "IP_ACL_BLOCKED",
  "SIGNED_OUT",
  "SIGN_OUT_INCOMPLETE",
  "PERMISSION_DENIED",
  "invalid_scope",
  "invalid_client",
  "unauthorized_client",
  "access_denied",
  "invalid_grant",
  "expired_token",
  "network",
  "timed_out",
  "browser_unavailable",
  "sign_in_failed",
]);
const SAFE_LABEL = /^[a-zA-Z][a-zA-Z0-9_-]{0,39}$/;
const SAFE_PHASES = new Set([
  "databricks",
  "oidc",
  "session_create",
  "workspace_landing",
  "recovery",
  "background",
  "authorization_code",
  "refresh_token",
]);
const SAFE_OUTCOMES = new Set([
  "ready",
  "loaded",
  "cancelled",
  "failed",
  "existing",
  "renewed",
  "browser",
  "signed_in",
  "accepted",
  "rejected",
]);
const SAFE_REQUEST_ID = /^[a-zA-Z0-9._:-]{1,128}$/;

/** A bounded, main-process-only record. Never serialize an Error, URL query or response body. */
function safeFields(fields) {
  const result = {};
  for (const [key, value] of Object.entries(fields ?? {})) {
    if (!ALLOWED_FIELDS.has(key)) continue;
    if (key === "origin") {
      try {
        const url = new URL(value);
        if (["http:", "https:"].includes(url.protocol)) result.origin = url.origin;
      } catch {
        // An invalid URL is not useful diagnostic context.
      }
    } else if (key === "status") {
      if (Number.isInteger(value) && value >= 100 && value <= 599) result.status = value;
    } else if (key === "interactive") {
      if (typeof value === "boolean") result.interactive = value;
    } else if (key === "requestId") {
      if (typeof value === "string" && SAFE_REQUEST_ID.test(value)) result.requestId = value;
    } else if (key === "code") {
      if (SAFE_CODES.has(value) || (typeof value === "string" && /^net_\d{1,5}$/.test(value))) {
        result.code = value;
      }
    } else if (key === "phase" && SAFE_PHASES.has(value)) {
      result.phase = value;
    } else if (key === "outcome" && SAFE_OUTCOMES.has(value)) {
      result.outcome = value;
    }
  }
  return result;
}

/** Keep diagnostics separate from stdout and untrusted renderer/CLI output. */
function createDiagnostics() {
  let directory = null;
  let writeFailed = false;
  const logPath = () => path.join(directory, LOG_NAME);

  function initialize(app) {
    try {
      app.setAppLogsPath();
      const logsDirectory = app.getPath("logs");
      fs.mkdirSync(logsDirectory, { recursive: true, mode: 0o700 });
      directory = logsDirectory;
      record("desktop_started", { outcome: "ready" });
    } catch (error) {
      console.warn("[omnigent] could not initialize desktop diagnostics", error.code);
    }
  }

  function record(event, fields = {}) {
    if (!directory || !SAFE_LABEL.test(event)) return;
    const line =
      JSON.stringify({ time: new Date().toISOString(), event, ...safeFields(fields) }) + "\n";
    try {
      const file = logPath();
      const size = fs.existsSync(file) ? fs.statSync(file).size : 0;
      if (size + Buffer.byteLength(line) > MAX_FILE_BYTES) {
        for (let index = BACKUPS; index > 0; index--) {
          const older = index === 1 ? file : `${file}.${index - 1}`;
          if (fs.existsSync(older)) fs.renameSync(older, `${file}.${index}`);
        }
      }
      fs.appendFileSync(file, line, { mode: 0o600 });
    } catch (error) {
      // Diagnostics must never break sign-in. Report once to the terminal.
      if (!writeFailed) console.warn("[omnigent] could not write desktop diagnostics", error.code);
      writeFailed = true;
    }
  }

  /** Export only our own bounded files; never include cookies, tokens or server/CLI logs. */
  function snapshot() {
    if (!directory) throw new Error("Desktop diagnostics are not initialized");
    const entries = [];
    for (let index = BACKUPS; index >= 0; index--) {
      const file = index === 0 ? logPath() : `${logPath()}.${index}`;
      try {
        const stats = fs.statSync(file);
        if (!stats.isFile() || stats.size > MAX_FILE_BYTES) continue;
        entries.push(fs.readFileSync(file, "utf8"));
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
    }
    return entries.join("");
  }

  function exportTo(destination) {
    if (!directory) throw new Error("Desktop diagnostics are not initialized");
    const logsDirectory = fs.realpathSync(directory);
    // Resolve both the containing directory and existing target: a save dialog
    // can return a path through a symlink to this log or one of its backups.
    let target;
    try {
      target = fs.realpathSync(destination);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      target = path.join(fs.realpathSync(path.dirname(destination)), path.basename(destination));
    }
    if (
      Array.from({ length: BACKUPS + 1 }, (_, index) =>
        path.join(logsDirectory, index === 0 ? LOG_NAME : `${LOG_NAME}.${index}`),
      ).includes(target)
    ) {
      throw new Error("Choose a destination outside the desktop diagnostics log files");
    }
    const content = snapshot();
    const temporary = path.join(
      path.dirname(destination),
      `.${path.basename(destination)}.${randomUUID()}.tmp`,
    );
    try {
      fs.writeFileSync(temporary, content, { encoding: "utf8", mode: 0o600, flag: "wx" });
      fs.renameSync(temporary, destination);
    } finally {
      try {
        fs.unlinkSync(temporary);
      } catch (error) {
        if (error.code !== "ENOENT")
          console.warn("[omnigent] could not remove diagnostics temp file", error.code);
      }
    }
  }

  return { initialize, record, snapshot, exportTo };
}

const diagnostics = createDiagnostics();

module.exports = { diagnostics, createDiagnostics, safeFields, LOG_NAME, MAX_FILE_BYTES };
