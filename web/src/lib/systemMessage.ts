// Detects and parses the `[System: ...]` user-role messages the runtime
// injects into conversations (task completion/failure/cancellation,
// timer firings, terminal-idle notifications, sub-agent wake notices).
// These are sent as
// role="user" because OpenAI-style chat formats lack a mid-conversation
// system-event role; the UI re-classifies them so they render as muted
// markers instead of normal user bubbles.

import type { MessageContentBlock } from "@/lib/blocks";
import { isTextBlock } from "@/lib/blocks";

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

// Claude Code's background-task wake, re-labelled by the client from the raw
// `<task-notification>` block the CLI injects (see `claudeTaskNotificationMarker`).
const BACKGROUND_TASK_RE = /^background task (\S+) (completed|failed|cancelled|finished)$/;
const TASK_NOTIFICATION_MARKERS = ["<task-notification>", "<task-id>", "</task-notification>"];

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
  const backgroundMatch = BACKGROUND_TASK_RE.exec(inner);
  if (backgroundMatch) {
    const status = backgroundMatch[2] ?? "finished";
    const kind: SystemMessageKind =
      status === "completed"
        ? "task_completed"
        : status === "failed"
          ? "task_failed"
          : status === "cancelled"
            ? "task_cancelled"
            : "generic";
    return { kind, label: `Background task ${status}`, body };
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
    .filter(isTextBlock)
    .map((c) => c.text)
    .join("")
    .replace(ATTACHED_RE, "")
    .trim();
  return parseSystemMessage(text) !== null;
}

/**
 * Whether ``text`` is the ``<task-notification>`` block Claude Code injects as
 * a user-role entry when a background task finishes.
 *
 * :param text: One user message text block.
 * :returns: ``true`` for a task notification, ``false`` otherwise.
 */
export function isClaudeTaskNotificationText(text: string): boolean {
  const trimmed = text.trimStart();
  return (
    trimmed.startsWith("<task-notification>") &&
    TASK_NOTIFICATION_MARKERS.every((marker) => trimmed.includes(marker))
  );
}

/**
 * Re-label a Claude Code ``<task-notification>`` block as a ``[System: …]``
 * marker. Claude resumes on the notification with no human message in
 * between; the marker keeps that resume visible as a turn boundary, so the
 * answer Claude had already finished is not folded into the follow-up work.
 *
 * :param text: One user message text block.
 * :returns: Marker text (header plus the notification's summary as the
 *   body), or ``null`` when ``text`` is not a task notification.
 */
export function claudeTaskNotificationMarker(text: string): string | null {
  if (!isClaudeTaskNotificationText(text)) return null;
  // The header regex takes the id as one token; an id with inner whitespace
  // would otherwise stop the marker parsing as a background task.
  const rawId = /<task-id>([^<]*)<\/task-id>/.exec(text)?.[1]?.trim() ?? "";
  const taskId = rawId !== "" && !/\s/.test(rawId) ? rawId : "unknown";
  const rawStatus = /<status>([^<]*)<\/status>/.exec(text)?.[1]?.trim().toLowerCase();
  const status =
    rawStatus === "completed" || rawStatus === "failed" || rawStatus === "cancelled"
      ? rawStatus
      : "finished";
  const summary = /<summary>([\s\S]*?)<\/summary>/.exec(text)?.[1]?.trim() ?? "";
  const header = `[System: background task ${taskId} ${status}]`;
  return summary ? `${header}\n${summary}` : header;
}

/**
 * System-marker content for a user message that is a Claude task notification.
 *
 * :param content: A user message block's content array.
 * :returns: One ``input_text`` block carrying the marker, or ``null`` for an
 *   ordinary user message.
 */
export function taskNotificationMarkerContent(
  content: MessageContentBlock[],
): MessageContentBlock[] | null {
  for (const block of content) {
    if (!isTextBlock(block)) continue;
    const marker = claudeTaskNotificationMarker(block.text);
    if (marker !== null) return [{ type: "input_text", text: marker }];
  }
  return null;
}
