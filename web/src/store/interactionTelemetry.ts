// Live interaction telemetry, emitted from the pump's SSE reduce-loop in
// chatStore.ts — the one place that sees each stream frame ONCE, live, in order.
// It is NOT derived from committed conversation state: that state is built for
// rendering and erases what telemetry needs (live-vs-history, an edge vs. a
// revived level transition, a real outcome), which mismeasured on every
// reconnect / revive / reopen. History hydration takes the `reduceSync` path,
// not this loop, so reopening a conversation never re-emits.
//
//   - agent_run      — onResponseStart / onResponseEnd (mapped terminal status).
//                      Revive reopens a turn via `set()` and emits neither block,
//                      so it never double-opens a run.
//   - tool_call      — onLiveBlock: start on a live tool_group; complete (NO
//                      status — none is reliable) on its result or when its run ends.
//   - create_session — markSessionCreated (create sites; they alone know the host
//                      kind) → onLiveBlock on first activity, else onResponseEnd fail.
//
// Spans still open when a conversation is disposed are settled by
// onConversationDisposed (wired to the registry's dispose notification).

import { startTimedInteraction, type TimedInteraction } from "@/lib/analyticsEmit";
import type { AnyBlock } from "@/lib/blocks";
import { conversationRegistry } from "./conversationRegistry";

type HostKind = "sandbox" | "computer";

interface ConvSpans {
  create?: TimedInteraction;
  run?: { responseId: string; handle: TimedInteraction };
  /** Open tool calls by callId (removed on their result). */
  tools: Map<string, TimedInteraction>;
}

const spans = new Map<string, ConvSpans>();

function spansFor(id: string): ConvSpans {
  let s = spans.get(id);
  if (s === undefined) {
    s = { tools: new Map() };
    spans.set(id, s);
  }
  return s;
}

// Map a response's terminal status to an interaction outcome.
const TERMINAL_OUTCOME: Record<string, "success" | "failure" | "cancelled"> = {
  completed: "success",
  failed: "failure",
  cancelled: "cancelled",
  incomplete: "cancelled", // interrupted / hit a limit
};

// The first assistant output the user perceives — in streaming or committed
// form (the native-harness path commits text as `text_done`, reasoning as
// `reasoning_block`). NOT the `user_message` echo or an `error` block.
const ACTIVITY_TYPES = new Set([
  "text_chunk",
  "text_done",
  "reasoning_chunk",
  "reasoning_block",
  "tool_group",
  "native_tool",
  "file",
]);

/**
 * Register a brand-new session for create_session timing. Called by the create
 * sites (the send/landing path and the New Chat dialog) — the only places that
 * know the host kind. Opens the span now; the pump completes it on the session's
 * first assistant activity, or settles it if the first turn fails without any.
 * Idempotent per session id.
 */
export function markSessionCreated(sessionId: string, hostKind: HostKind): void {
  const s = spansFor(sessionId);
  if (s.create !== undefined) return;
  s.create = startTimedInteraction(
    hostKind === "sandbox" ? "create_session_sandbox" : "create_session_computer",
    sessionId,
  );
}

/** A response turn started (the `response_start` block). Opens an agent_run. */
export function onResponseStart(conversationId: string, responseId: string): void {
  const s = spansFor(conversationId);
  if (s.run !== undefined) {
    if (s.run.responseId === responseId) return; // re-delivered response.created on reconnect
    s.run.handle.complete("cancelled"); // a prior run never saw its terminal
  }
  s.run = { responseId, handle: startTimedInteraction("agent_run", responseId) };
}

/**
 * A response turn reached a terminal (the `response_end` block). Completes its
 * agent_run and closes any tool left open by it. Settles a pending
 * create_session only on a genuine failure terminal: a `completed` edge can be a
 * stray idle that `reviveStrayCompletedResponse` reopens, and late content would
 * then complete the span correctly — failing on it would misreport a healthy
 * session as a failed create.
 */
export function onResponseEnd(conversationId: string, responseId: string, status: string): void {
  const s = spans.get(conversationId);
  if (s === undefined) return;
  const outcome = TERMINAL_OUTCOME[status] ?? "cancelled";

  if (s.run !== undefined && s.run.responseId === responseId) {
    s.run.handle.complete(outcome);
    s.run = undefined;
  }
  // A tool that never delivered a result before its run ended was abandoned by
  // that turn — close it (no status) rather than leak it.
  for (const handle of s.tools.values()) handle.complete(null);
  s.tools.clear();
  if (s.create !== undefined && outcome !== "success") {
    s.create.complete(outcome);
    s.create = undefined;
  }
}

/**
 * A block committed live to the transcript. Drives tool_call boundaries and
 * completes a pending create_session on the first assistant activity.
 */
export function onLiveBlock(conversationId: string, block: AnyBlock): void {
  if (block.type === "tool_group") {
    const s = spansFor(conversationId);
    for (const ex of block.executions) {
      if (ex.callId && !s.tools.has(ex.callId)) {
        s.tools.set(ex.callId, startTimedInteraction("tool_call", ex.callId, ex.name));
      }
    }
    completeCreateIfOpen(s); // a tool call is first activity too
    return;
  }

  const s = spans.get(conversationId);
  if (s === undefined) return;

  if (block.type === "tool_result") {
    const open = s.tools.get(block.callId);
    if (open !== undefined) {
      s.tools.delete(block.callId);
      open.complete(null); // no reliable success/failure signal on a tool result
    }
    return;
  }
  if (ACTIVITY_TYPES.has(block.type)) completeCreateIfOpen(s);
}

function completeCreateIfOpen(s: ConvSpans): void {
  if (s.create === undefined) return;
  s.create.complete("success");
  s.create = undefined;
}

// A disposed conversation delivers no more frames, so settle whatever it left
// open. Wired to the registry's dispose notification (release / evict / clear).
function onConversationDisposed(conversationId: string): void {
  const s = spans.get(conversationId);
  if (s === undefined) return;
  spans.delete(conversationId);
  if (s.run !== undefined) s.run.handle.complete("cancelled");
  for (const handle of s.tools.values()) handle.complete(null);
  if (s.create !== undefined) s.create.complete("cancelled");
}

conversationRegistry.subscribeDisposed(onConversationDisposed);

/**
 * Drop all in-flight tracking. Test-only: the span map is a module singleton, so
 * tests that drive the pump must reset it between cases.
 */
export function resetInteractionTelemetryForTests(): void {
  spans.clear();
}
