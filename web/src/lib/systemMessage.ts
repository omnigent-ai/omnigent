// Detects and parses the `[System: ...]` user-role messages the runtime
// injects into conversations (task completion/failure/cancellation,
// timer firings, terminal-idle notifications, sub-agent wake notices).
// These are sent as
// role="user" because OpenAI-style chat formats lack a mid-conversation
// system-event role; the UI re-classifies them so they render as muted
// markers instead of normal user bubbles.

import type { MessageContentBlock } from "@/lib/blocks";
import { isTextBlock } from "@/lib/blocks";

const TEAMMATE_MESSAGE_RE =
  /^<teammate-message\s+[^>]*\bteammate_id="[^"]+"[^>]*>[\s\S]*?<\/teammate-message>/;
const AGENT_MESSAGE_RE = /^<agent-message\s+[^>]*\bfrom="[^"]+"[^>]*>[\s\S]*?<\/agent-message>/;
const TEAMMATE_MESSAGE_PREFIXES = [
  "Another Claude session sent a message:\n",
  "Another Claude session sent a message while you were working:\n",
  "A peer session sent a message while you were working:\n",
];
const TEAMMATE_DELIVERY_GUIDANCE = [
  "This came from another Claude session — not typed by your user,",
  'That "other Claude session" is an agent working inside this same session —',
];

/** Recognize older Claude task/team context that was persisted without is_meta. */
export function isClaudeAgentMessageContent(content: MessageContentBlock[]): boolean {
  if (content.some((block) => block.type !== "input_text")) return false;
  const texts = content.map((block) => ("text" in block ? block.text.trim() : "")).filter(Boolean);
  return texts.length > 0 && texts.every(isClaudeAgentMessageText);
}

function isClaudeAgentMessageText(text: string): boolean {
  if (isClaudeTaskNotificationText(text)) {
    return /<summary>\s*Agent\b/.test(text) || /<result>[\s\S]*<\/result>/.test(text);
  }
  const prefix = TEAMMATE_MESSAGE_PREFIXES.find((candidate) => text.startsWith(candidate));
  let remaining = prefix ? text.slice(prefix.length).trimStart() : text;
  let match = TEAMMATE_MESSAGE_RE.exec(remaining) ?? AGENT_MESSAGE_RE.exec(remaining);
  if (!match) return false;
  while (match) {
    remaining = remaining.slice(match[0].length).trimStart();
    if (!remaining) return true;
    match = TEAMMATE_MESSAGE_RE.exec(remaining) ?? AGENT_MESSAGE_RE.exec(remaining);
  }
  // Peer wrappers can append native delivery instructions after the envelopes.
  return Boolean(
    prefix && TEAMMATE_DELIVERY_GUIDANCE.some((guidance) => remaining.startsWith(guidance)),
  );
}

/** One `<teammate-message>` envelope from a Claude agent-teams delivery. */
export interface TeammateDelivery {
  teammateId: string;
  summary: string | null;
  /** Envelope body; the raw `idle_notification` JSON when `idleResult` is set. */
  body: string;
  /** `result` of an `idle_notification` envelope; `null` for a prose message. */
  idleResult: string | null;
}

/** Identity of a readable teammate marker produced by `teammateDeliveryMarkerContent`. */
export interface TeammateMarker {
  teammateId: string;
  kind: "teammate_message" | "teammate_finished";
}

// Attribute values are quoted, so a `>` inside a summary stays in the tag.
const TEAMMATE_ENVELOPE_RE =
  /^<teammate-message\s+((?:[^>"]|"[^"]*")*)>([\s\S]*?)<\/teammate-message>/;
const TEAMMATE_ATTR_RE = /([A-Za-z_][\w-]*)="([^"]*)"/g;
// The summary branch comes first so a summary that is literally "finished" stays a summary.
const TEAMMATE_HEADER_RE = /^teammate ([^\s:]+)(?:: (.*)| (finished))?$/;

function idleNotificationResult(body: string): string | null {
  if (!body.startsWith("{")) return null;
  try {
    const decoded: unknown = JSON.parse(body);
    if (!decoded || typeof decoded !== "object") return null;
    const record = decoded as Record<string, unknown>;
    if (record.type !== "idle_notification") return null;
    return typeof record.result === "string" ? record.result.trim() : "";
  } catch {
    return null;
  }
}

/**
 * Parse Claude's framed agent-teams delivery ("Another Claude session sent a
 * message:" + envelopes, optionally followed by Claude's guidance) into its
 * envelopes; bare envelopes, human discussion, and incomplete markup yield `null`.
 */
export function parseTeammateDeliveries(text: string): TeammateDelivery[] | null {
  const prefix = TEAMMATE_MESSAGE_PREFIXES.find((candidate) => text.startsWith(candidate));
  if (!prefix) return null;
  let remaining = text.slice(prefix.length).trimStart();
  const deliveries: TeammateDelivery[] = [];
  let match = TEAMMATE_ENVELOPE_RE.exec(remaining);
  while (match) {
    const attrs = new Map<string, string>();
    for (const [, key, value] of match[1]!.matchAll(TEAMMATE_ATTR_RE)) attrs.set(key!, value!);
    const teammateId = attrs.get("teammate_id")?.trim();
    if (!teammateId) return null;
    const body = match[2]!.trim();
    deliveries.push({
      teammateId,
      summary: attrs.get("summary")?.trim() || null,
      body,
      idleResult: idleNotificationResult(body),
    });
    remaining = remaining.slice(match[0].length).trimStart();
    match = TEAMMATE_ENVELOPE_RE.exec(remaining);
  }
  if (deliveries.length === 0) return null;
  if (remaining && !TEAMMATE_DELIVERY_GUIDANCE.some((guidance) => remaining.startsWith(guidance))) {
    return null;
  }
  return deliveries;
}

function teammateMarkerHeader(
  teammateId: string,
  summary: string | null,
  finished: boolean,
): string {
  // The header regex takes the id as one token and keeps the summary on its line.
  const id = teammateMarkerId(teammateId);
  if (finished) return `[System: teammate ${id} finished]`;
  const line = summary?.replace(/\s+/g, " ").trim();
  return line ? `[System: teammate ${id}: ${line}]` : `[System: teammate ${id}]`;
}

function teammateMarkerId(teammateId: string): string {
  return teammateId.replace(/[\s:]+/g, "-");
}

/** A framed delivery re-labelled as a marker, with the identity the marker carries. */
export interface TeammateDeliveryMarker {
  content: MessageContentBlock[];
  marker: TeammateMarker;
}

/**
 * Re-label a framed teammate delivery as a readable marker: the prose body with
 * its `summary`, or the result an idle notification carries. An idle twin that
 * trails a prose message from the same teammate only restates it and is folded
 * away; a result-less idle ping has nothing to show. `null` for anything else.
 */
export function teammateDeliveryMarker(
  content: MessageContentBlock[],
): TeammateDeliveryMarker | null {
  if (content.some((block) => !isTextBlock(block))) return null;
  const texts = content
    .filter(isTextBlock)
    .map((block) => block.text.trim())
    .filter(Boolean);
  if (texts.length === 0) return null;
  const deliveries: TeammateDelivery[] = [];
  for (const text of texts) {
    const parsed = parseTeammateDeliveries(text);
    if (parsed === null) return null;
    deliveries.push(...parsed);
  }
  const shown = deliveries.filter((delivery, index) => {
    if (delivery.idleResult === null) return true;
    if (!delivery.idleResult) return false;
    return !deliveries
      .slice(0, index)
      .some((other) => other.teammateId === delivery.teammateId && other.idleResult === null);
  });
  // A prose message heads the marker when there is one, so a delivery that
  // also carries another teammate's finish is not titled as a finish.
  const lead = shown.find((delivery) => delivery.idleResult === null) ?? shown[0];
  if (lead === undefined) return null;
  const header = teammateMarkerHeader(lead.teammateId, lead.summary, lead.idleResult !== null);
  const lines = [lead.idleResult ?? lead.body];
  for (const delivery of shown) {
    if (delivery === lead) continue;
    lines.push(
      delivery.idleResult === null
        ? `@${delivery.teammateId}: ${delivery.body}`
        : `@${delivery.teammateId} finished: ${delivery.idleResult}`,
    );
  }
  const body = lines.filter(Boolean).join("\n\n");
  return {
    content: [{ type: "input_text", text: body ? `${header}\n${body}` : header }],
    marker: {
      teammateId: teammateMarkerId(lead.teammateId),
      kind: lead.idleResult === null ? "teammate_message" : "teammate_finished",
    },
  };
}

/** Marker content of a framed delivery; `null` for anything else. */
export function teammateDeliveryMarkerContent(
  content: MessageContentBlock[],
): MessageContentBlock[] | null {
  return teammateDeliveryMarker(content)?.content ?? null;
}

/** Identify a teammate marker in user-message content; `null` for anything else. */
export function teammateMarkerOf(content: MessageContentBlock[]): TeammateMarker | null {
  if (content.some((block) => !isTextBlock(block))) return null;
  const text = content
    .filter(isTextBlock)
    .map((block) => block.text)
    .join("\n")
    .trim();
  const parsed = parseSystemMessage(text);
  if (!parsed?.teammate) return null;
  if (parsed.kind !== "teammate_message" && parsed.kind !== "teammate_finished") return null;
  return { teammateId: parsed.teammate.id, kind: parsed.kind };
}

export type SystemMessageKind =
  | "task_completed"
  | "task_failed"
  | "task_cancelled"
  | "timer_fired"
  | "terminal_idle"
  | "subagent_wake"
  | "teammate_message"
  | "teammate_finished"
  | "interrupted"
  | "generic";

export interface ParsedSystemMessage {
  kind: SystemMessageKind;
  /** Human-readable label, no opaque ids. e.g. "Sub-agent completed". */
  label: string;
  /** Everything after the header line. Empty for headers without a body. */
  body: string;
  /** Set for the teammate kinds: who sent the delivery and its one-line summary. */
  teammate?: { id: string; summary: string | null };
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
  const teammateMatch = TEAMMATE_HEADER_RE.exec(inner);
  if (teammateMatch) {
    const [, id, summary, finished] = teammateMatch;
    const teammate = { id: id!, summary: finished ? null : (summary ?? null) };
    return finished
      ? { kind: "teammate_finished", label: `Teammate ${id} finished`, body, teammate }
      : { kind: "teammate_message", label: `Teammate ${id}`, body, teammate };
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
  const trimmed = text.trim();
  return (
    /^<task-notification>[\s\S]*<\/task-notification>$/.test(trimmed) &&
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
  if (content.some((block) => !isTextBlock(block))) return null;
  if (isClaudeAgentMessageContent(content)) return null;
  if (content.some((block) => isTextBlock(block) && !isClaudeTaskNotificationText(block.text))) {
    return null;
  }
  for (const block of content) {
    if (!isTextBlock(block)) continue;
    const marker = claudeTaskNotificationMarker(block.text);
    if (marker !== null) return [{ type: "input_text", text: marker }];
  }
  return null;
}
