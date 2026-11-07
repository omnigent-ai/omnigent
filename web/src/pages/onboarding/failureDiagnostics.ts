// Copyable diagnostics for a failed onboarding step.
//
// Built only from an allowlist: the curated failure and next-step copy the shell
// already produced, a short failure kind, the stage, the runner, and the server
// origin. Raw subprocess output is never an input, and even the curated copy is
// redacted so a ticket, OAuth URL or home path cannot reach the clipboard. The
// Electron twin is `electron/src/failure_diagnostics.js`; keep the formats in sync.

const MAX_FIELD_CHARS = 300;
const KIND_RE = /^[a-z0-9][a-z0-9:-]{0,47}$/;

export interface FailureDiagnosticsInput {
  /** Screen the failure appeared on. */
  surface: string;
  stage?: string;
  runner?: string;
  errorKind?: unknown;
  /** Curated failure copy. Absent for legacy results that only carry `error`. */
  errorMessage?: unknown;
  errorAction?: unknown;
  serverUrl?: unknown;
  /** Developer fixtures label themselves so a copy is never mistaken for a live failure. */
  simulated?: boolean;
  now?: () => Date;
}

/** Strip anything that could carry auth material, then bound the length. */
export function redactDiagnosticText(value: unknown): string {
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

function serverOrigin(serverUrl: unknown): string {
  if (typeof serverUrl !== "string") return "";
  try {
    const { origin } = new URL(serverUrl);
    return origin === "null" ? "" : origin;
  } catch {
    return "";
  }
}

export function buildFailureDiagnostics(input: FailureDiagnosticsInput): string {
  const kind =
    typeof input.errorKind === "string" && KIND_RE.test(input.errorKind)
      ? input.errorKind
      : "unknown";
  const failure = redactDiagnosticText(input.errorMessage);
  const action = redactDiagnosticText(input.errorAction);
  const server = serverOrigin(input.serverUrl);
  return [
    "Omnigent desktop failure diagnostics",
    ...(input.simulated ? ["Simulated: yes (developer fixture, not a live failure)"] : []),
    `Surface: ${redactDiagnosticText(input.surface)}`,
    ...(input.stage ? [`Stage: ${redactDiagnosticText(input.stage)}`] : []),
    ...(input.runner ? [`Runner: ${redactDiagnosticText(input.runner)}`] : []),
    `Failure kind: ${kind}`,
    `Failure: ${failure || "details withheld (no curated message)"}`,
    ...(action ? [`Next step: ${action}`] : []),
    ...(server ? [`Server: ${server}`] : []),
    `Time: ${(input.now?.() ?? new Date()).toISOString()}`,
  ].join("\n");
}
