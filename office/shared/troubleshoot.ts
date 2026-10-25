// Diagnosis vocabulary for failing role sessions, shared by the service and the UI.

import type { AgentRoleId } from "./sdlcTheme.ts";

export type ProblemCategory =
  | "rate_limit"
  | "usage_limit"
  | "auth"
  | "harness_setup"
  | "harness_crash"
  | "runner"
  | "host_offline"
  | "upstream"
  | "context"
  | "stalled"
  | "no_response"
  | "start_failed"
  | "missing"
  | "unknown";

/**
 * `continue` resends the turn as Omnigent's own retry does; `recover` reconnects or
 * relaunches the runner; `nudge` reminds the agent what it was asked to do.
 */
export type RecoveryAction = "continue" | "recover" | "nudge" | "retry_later" | "switch_harness" | "restart" | "start" | "stop";

export type HarnessReadiness = "ready" | "needs-auth" | "binary-missing" | "version-too-low" | "unconfigured" | "unknown";

export interface FailureDetail {
  code: string;
  message: string;
  source?: string;
  title?: string;
  cause?: string;
  remediation?: string;
  /** Epoch seconds the failure was recorded, when known. */
  at?: number;
}

export interface RoleProblem {
  /** Stable for one occurrence, so a dismissal or notification is not repeated. */
  key: string;
  category: ProblemCategory;
  title: string;
  /** What Omnigent or the office observed, verbatim where possible. */
  detail: string;
  cause: string | null;
  remediation: string | null;
  code: string | null;
  since: number | null;
  actions: RecoveryAction[];
}

export interface ScheduledRetry {
  sessionId: string;
  branchId: string;
  action: "continue" | "recover";
  nextAt: number;
  /** Minutes between attempts while the problem persists; null retries once. */
  repeatMinutes: number | null;
  attempts: number;
  lastResult: string | null;
}

export interface TroubleshootState {
  /** Problem key the PO chose to ignore, per session (or `${branchId}:${role}` without one). */
  dismissed: Record<string, string>;
  retries: Record<string, ScheduledRetry>;
  /** Last problem key a desktop notification was sent for, keyed like `dismissed`. */
  notified: Record<string, string>;
}

export function emptyTroubleshootState(): TroubleshootState {
  return { dismissed: {}, retries: {}, notified: {} };
}

export type RoleHealthState = "working" | "waiting_human" | "idle" | "problem" | "not_started";

export interface RoleHealth {
  role: AgentRoleId;
  sessionId: string | null;
  temporary: boolean;
  harness: string;
  model: string | null;
  harnessReadiness: HarnessReadiness;
  state: RoleHealthState;
  problem: RoleProblem | null;
  /** The PO chose to ignore this occurrence; it no longer flags the room. */
  dismissed: boolean;
  scheduledRetry: ScheduledRetry | null;
}

export const PROBLEM_TITLES: Record<ProblemCategory, string> = {
  rate_limit: "Rate limit reached",
  usage_limit: "Out of tokens or usage quota",
  auth: "Signed out or credentials expired",
  harness_setup: "Harness not set up on the host",
  harness_crash: "The harness stopped unexpectedly",
  runner: "Lost the connection to the agent runtime",
  host_offline: "The host is offline",
  upstream: "The model service had a temporary error",
  context: "The conversation is too long for the model",
  stalled: "No activity for a long time",
  no_response: "The agent stopped before doing what it was asked",
  start_failed: "The role session could not start",
  missing: "The role session no longer exists in Omnigent",
  unknown: "The agent's turn failed",
};

export const DEFAULT_REMEDIATION: Partial<Record<ProblemCategory, string>> = {
  rate_limit: "Retry later, or move this role to another harness.",
  usage_limit: "Move this role, or every role on this harness, to another harness, or retry after the quota resets.",
  auth: "Sign in to the harness on the host, then choose Continue.",
  harness_setup: "Install or configure the harness on the host, or move this role to another harness.",
  harness_crash: "Reconnect the runtime. If it fails again, restart the role with a handoff.",
  runner: "Reconnect the runtime. If it fails again, restart the role with a handoff.",
  host_offline: "Start `omnigent host` on that machine, then reconnect.",
  upstream: "Continue the turn; temporary errors usually clear on retry.",
  context: "Restart the role: the new session gets a short handoff instead of the long history.",
  stalled: "Stop the turn, or remind the agent what it was doing.",
  no_response: "Remind the agent, or restart it with a handoff.",
  start_failed: "Fix the cause, then start the role again, or move it to another harness.",
  missing: "Start a new session for the role with a handoff.",
};

export const ACTION_LABELS: Record<RecoveryAction, string> = {
  continue: "Continue the turn",
  recover: "Reconnect runtime",
  nudge: "Remind the agent",
  retry_later: "Retry later",
  switch_harness: "Switch harness",
  restart: "Restart with handoff",
  start: "Start the role",
  stop: "Stop the turn",
};

const RECOMMENDED_ACTIONS: Record<ProblemCategory, RecoveryAction[]> = {
  rate_limit: ["retry_later", "continue", "switch_harness"],
  usage_limit: ["switch_harness", "retry_later", "continue"],
  auth: ["continue", "switch_harness", "restart"],
  harness_setup: ["switch_harness", "recover", "restart"],
  harness_crash: ["recover", "restart", "switch_harness"],
  runner: ["recover", "restart", "switch_harness"],
  host_offline: ["recover", "retry_later"],
  upstream: ["continue", "retry_later", "switch_harness"],
  context: ["restart", "switch_harness"],
  stalled: ["stop", "nudge", "restart"],
  no_response: ["nudge", "restart", "switch_harness"],
  start_failed: ["start", "switch_harness"],
  missing: ["restart"],
  unknown: ["continue", "recover", "restart", "switch_harness"],
};

export function recommendedActions(category: ProblemCategory): RecoveryAction[] {
  return RECOMMENDED_ACTIONS[category];
}

const USAGE_LIMIT_TEXT =
  /quota|credit balance|out of credits|insufficient[_ ]credits|usage limit|budget|billing|spending limit|exceeded your (current )?plan|token limit reached|tokens? (are )?exhausted/i;
const RATE_LIMIT_TEXT = /rate[_ -]?limit|too many requests|\b429\b|overloaded|try again in \d+/i;
const AUTH_TEXT =
  /\b401\b|\b403\b|unauthori[sz]ed|forbidden|invalid (api )?key|api key|not (logged|signed) in|sign[_ -]?in|log ?in required|reauth|expired (token|credentials|session)|authenticat/i;
const CONTEXT_TEXT = /context[_ ]length|context window|too many tokens|prompt is too long|request too large|maximum context/i;
const SETUP_TEXT = /not configured|not installed|binary[_ -]missing|command not found|no such file|on path|update (it|the cli)|too old/i;
const UPSTREAM_TEXT = /\b50[234]\b|timed? ?out|temporar|unavailable|connection (reset|refused|closed)|ECONNRESET|stream (ended|closed)/i;

const CODE_CATEGORIES: Record<string, ProblemCategory> = {
  rate_limit_exceeded: "rate_limit",
  budget_exhausted: "usage_limit",
  insufficient_quota: "usage_limit",
  transient_upstream_error: "upstream",
  connection_error: "upstream",
  context_length_exceeded: "context",
  harness_not_configured: "harness_setup",
  client_update_required: "harness_setup",
  runner_disconnected: "runner",
  runner_failed_to_start: "runner",
  runner_unavailable: "runner",
  wrong_replica: "runner",
  native_terminal_start_failed: "harness_crash",
  terminal_launch_failed: "harness_crash",
  required_terminal_exited: "harness_crash",
};

/** Omnigent's own code wins; the message text classifies generic codes. */
export function classifyFailure(failure: Pick<FailureDetail, "code" | "message">): ProblemCategory {
  const code = failure.code.toLowerCase();
  const known = CODE_CATEGORIES[code];
  if (known) return known;
  const text = `${code} ${failure.message}`;
  // A spent quota is often reported with a 429 or 403, so it is checked first.
  if (USAGE_LIMIT_TEXT.test(text)) return "usage_limit";
  if (RATE_LIMIT_TEXT.test(text)) return "rate_limit";
  if (code.includes("auth") || code.includes("sign_in") || AUTH_TEXT.test(text)) return "auth";
  if (CONTEXT_TEXT.test(text)) return "context";
  if (SETUP_TEXT.test(text)) return "harness_setup";
  if (UPSTREAM_TEXT.test(text)) return "upstream";
  return "unknown";
}

// Spellings Omnigent folds before reading a host's harness report.
const HARNESS_ALIASES: Record<string, string> = {
  agy: "antigravity",
  "google-antigravity": "antigravity",
  claude: "claude-sdk",
  "github-copilot": "copilot",
};

/** A host's `configured_harnesses` entry for a harness; a missing report is unknown. */
export function harnessReadiness(harness: string, report: Record<string, unknown> | null | undefined): HarnessReadiness {
  if (!report || Object.keys(report).length === 0) return "unknown";
  const value = report[harness] ?? report[HARNESS_ALIASES[harness] ?? harness];
  if (value === true) return "ready";
  if (value === false || value === undefined || value === null) return "unconfigured";
  if (value === "needs-auth" || value === "binary-missing" || value === "version-too-low") return value;
  return "unknown";
}

export const READINESS_LABELS: Record<HarnessReadiness, string> = {
  ready: "ready",
  "needs-auth": "needs sign-in",
  "binary-missing": "not installed",
  "version-too-low": "needs an update",
  unconfigured: "not set up",
  unknown: "status unknown",
};
