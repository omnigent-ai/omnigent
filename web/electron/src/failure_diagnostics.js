"use strict";

/**
 * Copyable diagnostics for a failed desktop sign-in or Arca connect.
 *
 * Built only from an allowlist: the curated failure and next-step copy the shell
 * already produced, a short failure kind, the stage, the runner, and the server
 * origin. Raw subprocess output is never an input. The curated copy is still
 * redacted, because a server URL, home path or ticket must not reach a clipboard
 * even if upstream copy changes. The web twin is
 * `web/src/pages/onboarding/failureDiagnostics.ts`; keep the two formats in sync.
 */

const MAX_FIELD_CHARS = 300;
const KIND_RE = /^[a-z0-9][a-z0-9:-]{0,47}$/;

/**
 * @param {unknown} value
 * @returns {string}
 */
function redact(value) {
  if (typeof value !== "string") return "";
  const text = value
    // eslint-disable-next-line no-control-regex
    .replace(/\u001b\[[0-9;?]*[ -/]*[@-~]/g, "")
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, " ")
    // A URL keeps only its origin: path, query, fragment and userinfo can carry tickets.
    .replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s)`'"<>]+/gi, (match) => {
      try {
        const { origin } = new URL(match);
        return origin === "null" ? "<url>" : origin;
      } catch {
        return "<url>";
      }
    })
    .replace(/\/(?:home|Users)\/[^/\s`'")]+/g, "~")
    .replace(/[A-Za-z]:\\Users\\[^\\\s`'")]+/g, "~")
    .replace(/\b(Bearer|Basic)\s+\S+/gi, "$1 <redacted>")
    .replace(/\b(ticket|token|secret|password|passwd|code|key|signature)=\S+/gi, "$1=<redacted>")
    .replace(/\b[A-Za-z0-9_-]{32,}\b/g, "<redacted>")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > MAX_FIELD_CHARS ? `${text.slice(0, MAX_FIELD_CHARS - 1)}…` : text;
}

/**
 * @param {unknown} serverUrl
 * @returns {string}
 */
function serverOrigin(serverUrl) {
  if (typeof serverUrl !== "string") return "";
  try {
    const { origin } = new URL(serverUrl);
    return origin === "null" ? "" : origin;
  } catch {
    return "";
  }
}

/**
 * @param {{
 *   surface: string,
 *   stage?: string,
 *   runner?: string,
 *   errorKind?: unknown,
 *   errorMessage?: unknown,
 *   errorAction?: unknown,
 *   serverUrl?: unknown,
 *   simulated?: boolean,
 *   now?: () => Date,
 * }} input
 * @returns {string}
 */
function buildFailureDiagnostics(input) {
  const kind =
    typeof input.errorKind === "string" && KIND_RE.test(input.errorKind)
      ? input.errorKind
      : "unknown";
  const failure = redact(input.errorMessage);
  const action = redact(input.errorAction);
  const server = serverOrigin(input.serverUrl);
  const lines = [
    "Omnigent desktop failure diagnostics",
    ...(input.simulated ? ["Simulated: yes (developer fixture, not a live failure)"] : []),
    `Surface: ${redact(input.surface)}`,
    ...(input.stage ? [`Stage: ${redact(input.stage)}`] : []),
    ...(input.runner ? [`Runner: ${redact(input.runner)}`] : []),
    `Failure kind: ${kind}`,
    `Failure: ${failure || "details withheld (no curated message)"}`,
    ...(action ? [`Next step: ${action}`] : []),
    ...(server ? [`Server: ${server}`] : []),
    `Time: ${(input.now?.() ?? new Date()).toISOString()}`,
  ];
  return lines.join("\n");
}

module.exports = { buildFailureDiagnostics, redact, serverOrigin };
