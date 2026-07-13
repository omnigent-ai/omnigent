// Detects and parses the `[System: ...]` user-role messages the runtime
// injects into conversations (task completion/failure/cancellation,
// timer firings, terminal-idle notifications, sub-agent wake notices).
// These are sent as
// role="user" because OpenAI-style chat formats lack a mid-conversation
// system-event role; the UI re-classifies them so they render as muted
// markers instead of normal user bubbles.

import type { MessageContentBlock } from "@/lib/blocks";

export type SystemMessageKind =
  | "task_completed"
  | "task_failed"
  | "task_cancelled"
  | "timer_fired"
  | "terminal_idle"
  | "subagent_wake"
  | "interrupted"
  | "generic";

export interface ParsedSystemMessage {
  kind: SystemMessageKind;
  /** Human-readable label, no opaque ids. e.g. "Sub-agent completed". */
  label: string;
  /** Everything after the header line. Empty for headers without a body. */
  body: string;
}

const HEADER_RE = /^\[System: (.+)\]$/;
// Claude Code's own interrupt record (Escape), mirrored from its transcript.
// Not a `[System: ...]` marker, but we re-classify it the same way: a muted
// "Interrupted" indicator instead of a raw user bubble. Keep this exact so a
// user's bracketed question such as `[Request interrupted by user?]` still
// renders as normal text.
const INTERRUPT_RE = /^\[Request interrupted by user(?: for tool use)?\]$/;
const TASK_RE = /^task (\S+) \((tool|sub_agent|client_tool)\) (completed|failed|cancelled)$/;
const TIMER_RE = /^timer (\S+) fired$/;
const TERMINAL_RE = /^terminal (\S+) is idle$/;
const SUBAGENT_WAKE_RE =
  /^sub-agent .+ finished \((completed|failed|cancelled)\) — \d+ results? waiting in inbox\. Call sys_read_inbox to collect\.$/;

const TASK_KIND_LABEL: Record<string, string> = {
  tool: "Tool",
  sub_agent: "Sub-agent",
  client_tool: "Client tool",
};

export function parseSystemMessage(text: string): ParsedSystemMessage | null {
  const newlineIdx = text.indexOf("\n");
  const firstLine = newlineIdx === -1 ? text : text.slice(0, newlineIdx);
  const body = newlineIdx === -1 ? "" : text.slice(newlineIdx + 1);

  if (INTERRUPT_RE.test(firstLine)) {
    return { kind: "interrupted", label: "Interrupted", body: "" };
  }

  const headerMatch = HEADER_RE.exec(firstLine);
  if (!headerMatch) return null;
  const inner = headerMatch[1];

  // The runner synthesizes `[System: interrupted]` for codex-native (which
  // writes no interrupt record of its own); render it the same as Claude's.
  if (inner === "interrupted") {
    return { kind: "interrupted", label: "Interrupted", body };
  }

  const taskMatch = TASK_RE.exec(inner);
  if (taskMatch) {
    const [, taskId, taskKind, status] = taskMatch;
    const kindLabel = TASK_KIND_LABEL[taskKind] ?? taskKind;
    if (status === "completed") {
      return {
        kind: "task_completed",
        label: `${kindLabel} ${taskId} completed`,
        body,
      };
    }
    if (status === "failed") {
      return {
        kind: "task_failed",
        label: `${kindLabel} ${taskId} failed`,
        body,
      };
    }
    return {
      kind: "task_cancelled",
      label: `${kindLabel} ${taskId} cancelled`,
      body: "",
    };
  }
  const timerMatch = TIMER_RE.exec(inner);
  if (timerMatch) {
    return {
      kind: "timer_fired",
      label: `Timer ${timerMatch[1]} fired`,
      body,
    };
  }
  const terminalMatch = TERMINAL_RE.exec(inner);
  if (terminalMatch) {
    return {
      kind: "terminal_idle",
      label: `Terminal ${terminalMatch[1]} idle`,
      body: "",
    };
  }
  if (SUBAGENT_WAKE_RE.test(inner)) {
    return {
      kind: "subagent_wake",
      label: "Sub-agent result ready",
      body,
    };
  }
  // Known prefix, unknown pattern — still treat as a system marker so new
  // producers get the muted styling without an web change.
  return { kind: "generic", label: inner, body };
}

// Matches ChatPage's inline "[Attached: …]" marker stripper; the header check
// must see the same text extraction the bubble render uses.
const ATTACHED_RE = /\[Attached(?: file)?:\s*([^\]]*)\]\s*/g;

/**
 * True when a user-role message is actually a runtime `[System: …]` marker
 * (task/timer/sub-agent notice, interrupt record) rather than a real user
 * turn. Shared by the transcript's turn derivation and the rail's eager
 * history loader so both agree on what counts as a rail tick — a mismatch
 * (loader counting markers the rail drops) can wedge the rail hidden.
 *
 * :param content: A user message block's content array.
 * :returns: ``true`` for a system marker, ``false`` for a real user message.
 */
export function isSystemUserContent(content: MessageContentBlock[]): boolean {
  const hasAttachments = content.some((c) => c.type === "input_image" || c.type === "input_file");
  if (hasAttachments) return false;
  const text = content
    .filter(
      (c): c is Extract<MessageContentBlock, { type: "input_text" }> => c.type === "input_text",
    )
    .map((c) => c.text)
    .join("")
    .replace(ATTACHED_RE, "")
    .trim();
  return parseSystemMessage(text) !== null;
}
