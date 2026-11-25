import type { RenderItem, ToolState } from "@/lib/renderItems";
import { splitToolName } from "@/lib/toolTitle";
import {
  type ParsedToolOutput,
  isRecord,
  parseToolOutput,
  toolErrorMessage,
} from "@/lib/toolValue";

// ``sys_session_message`` prefixes each delivered message with its sender's id
// (``orchestration_message`` in omnigent/server/session_orchestration.py).
const SESSION_MESSAGE_PREFIX_RE = /^\[Message from session ([A-Za-z0-9_-]+)\]\r?\n/;

/** A message another session sent, split into its sender and its text. */
export interface SessionMessageOrigin {
  sourceId: string;
  body: string;
}

/**
 * Split the sender prefix off a message delivered by ``sys_session_message``.
 *
 * The prefix is a display label written by the server, not proof of origin.
 *
 * :param text: Message text, e.g. ``"[Message from session 9b8b…]\nhi"``.
 * :returns: ``{sourceId: "9b8b…", body: "hi"}``, or ``null`` without the prefix.
 */
export function splitSessionMessageOrigin(text: string): SessionMessageOrigin | null {
  const match = SESSION_MESSAGE_PREFIX_RE.exec(text);
  if (!match) return null;
  return { sourceId: match[1], body: text.slice(match[0].length) };
}

/**
 * How far a message this session sent got, as far as its tool result proves:
 * ``sent`` means the server accepted it, not that the recipient read it.
 */
export type OutboundSessionMessageStatus = "sending" | "sent" | "not_sent" | "unconfirmed";

/** A message this session sent to another session. */
export interface OutboundSessionMessage {
  /** The destination session, e.g. ``"9b8b26c8edfd40d781c0d13a04fa5c0b"``. */
  recipientId: string;
  body: string;
  status: OutboundSessionMessageStatus;
  /** The send's reported error, e.g. ``"Session message refused"``; ``null`` without one. */
  error: string | null;
}

interface SendOutcome {
  status: OutboundSessionMessageStatus;
  error: string | null;
}

// Placements whose new session receives its first task as a message from this
// session; a child's first task is ordinary sub-agent work instead.
const INDEPENDENT_PLACEMENTS = new Set(["same_host", "host", "managed"]);

/**
 * The message a ``sys_session_message`` call, or an independent ``sys_session_create``
 * with a first task, sent to another session; ``null`` for any other render item.
 *
 * Only a boolean ``queued`` acknowledgement counts as sent, and only a policy denial or
 * a ``delivery: "refused"`` error as not sent. Any other error, and a call whose turn
 * ended without a result, may already have been queued, so it stays unconfirmed.
 *
 * :param item: A transcript render item, e.g. a ``sys_session_message`` tool call.
 * :returns: E.g. ``{recipientId: "9b8b…", body: "Fix it", status: "sent", error: null}``.
 */
export function outboundSessionMessage(item: RenderItem): OutboundSessionMessage | null {
  if (item.kind !== "tool") return null;
  const { server, tool } = splitToolName(item.execution.name);
  if (server !== undefined) return null;
  const args = item.execution.arguments;
  if (tool === "sys_session_message") return sentMessage(args, item.output, item.state);
  if (tool === "sys_session_create") return createdSessionFirstTask(args, item.output);
  return null;
}

function sentMessage(
  args: Record<string, unknown>,
  output: string | null,
  state: ToolState,
): OutboundSessionMessage | null {
  const recipientId = nonBlankString(args.session_id);
  const body = nonBlankString(args.message);
  if (recipientId === null || body === null) return null;
  if (output !== null) {
    return { recipientId, body, ...acknowledgementOutcome(parseToolOutput(output)) };
  }
  const status = state === "input-available" ? "sending" : "unconfirmed";
  return { recipientId, body, status, error: null };
}

/** A created session's first task, once the create result names the new session. */
function createdSessionFirstTask(
  args: Record<string, unknown>,
  output: string | null,
): OutboundSessionMessage | null {
  const body = nonBlankString(args.message);
  const placement = args.placement;
  if (body === null || typeof placement !== "string" || !INDEPENDENT_PLACEMENTS.has(placement)) {
    return null;
  }
  if (output === null) return null;
  const parsed = parseToolOutput(output);
  const handle = parsed.kind === "json" && isRecord(parsed.value) ? parsed.value : null;
  const recipientId = nonBlankString(handle?.conversation_id);
  if (handle === null || recipientId === null) return null;
  // Same-host and host creates report the first task's own acknowledgement; a
  // managed create carries the task in its request, so creating the session accepted it.
  const initial = handle.initial_message;
  if (isRecord(initial)) {
    const acknowledgement: ParsedToolOutput = {
      kind: "json",
      value: initial,
      text: JSON.stringify(initial),
    };
    return { recipientId, body, ...acknowledgementOutcome(acknowledgement) };
  }
  const error = toolErrorMessage(parsed);
  return { recipientId, body, status: error === null ? "sent" : "unconfirmed", error };
}

function acknowledgementOutcome(parsed: ParsedToolOutput): SendOutcome {
  const acknowledgement = parsed.kind === "json" && isRecord(parsed.value) ? parsed.value : null;
  const error = toolErrorMessage(parsed);
  if (acknowledgement?.denied === true || acknowledgement?.delivery === "refused") {
    return { status: "not_sent", error: error ?? nonBlankString(acknowledgement.reason) };
  }
  if (error !== null) return { status: "unconfirmed", error };
  const accepted = typeof acknowledgement?.queued === "boolean";
  return { status: accepted ? "sent" : "unconfirmed", error: null };
}

function nonBlankString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}
