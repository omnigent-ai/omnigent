// Mirrors sdks/python-client/omnigent_client/_sse.py.
//
// Parses raw `text/event-stream` bytes into typed `StreamEvent`
// values. Output of this module is the input to `BlockStream.reduce()`.
//
// Hand-ported. When _sse.py changes (new event types, new shapes),
// update this file. There is no parity test here; bugs surface as the
// reducer never seeing certain events, which the reducer's tests can't
// catch — please add an SSE-parser test when you touch this.

import type {
  ClientTaskCancel,
  CompactionCompleted,
  CompactionFailed,
  CompactionInProgress,
  ElicitationRequest,
  ElicitationResolved,
  ErrorEvent,
  MessageDone,
  NativeToolCall,
  OutputFileDone,
  PolicyDenied,
  ReasoningDelta,
  ReasoningStarted,
  ReasoningSummaryDelta,
  ResponseCancelled,
  ResponseCompleted,
  ResponseCreated,
  ResponseFailed,
  ResponseIncomplete,
  ResponseInProgress,
  ResponseQueued,
  RetryEvent,
  SessionChangedFilesInvalidatedEvent,
  SessionChildSessionUpdatedEvent,
  SessionModelOptionsEvent,
  SessionCreatedEvent,
  SessionInputConsumedEvent,
  SessionInterruptedEvent,
  SessionPresenceEvent,
  SessionResource,
  SessionResourceCreatedEvent,
  SessionResourceDeletedEvent,
  SessionSkillsEvent,
  SessionViewer,
  SessionTerminalActivityEvent,
  SessionStatusEvent,
  SessionModelEvent,
  SessionCollaborationModeEvent,
  SessionReasoningEffortEvent,
  SessionModeEvent,
  SessionAgentChangedEvent,
  SessionTodosEvent,
  SessionSandboxStatusEvent,
  SessionTerminalPendingEvent,
  SessionUsageEvent,
  SlashCommand,
  TerminalCommandEvent,
  StreamEvent,
  TextDelta,
  ToolCall,
  ToolResult,
} from "./events";
import { NATIVE_TOOL_TYPES } from "./events";
import type { ErrorInfo, ModelUsage, Response } from "./types";

/**
 * Out-param for `parseSseStream`: `sawDone` is set when the server's `[DONE]`
 * sentinel is consumed (a deliberate close), distinguishing it from a
 * transport drop (stream ended/errored without `[DONE]`) that callers reconnect on.
 */
export interface SseStreamResult {
  sawDone: boolean;
}

/**
 * Parse an SSE byte stream into typed events.
 *
 * The byte stream is the body of `fetch("/v1/sessions/{id}/stream")`
 * (the session live-tail) — exposed in the browser as
 * `response.body` (a `ReadableStream<Uint8Array>`). The parser is
 * agnostic to the upstream endpoint; events carry both the
 * `response.*` (task-scoped) and `session.*` (session-scoped)
 * vocabularies.
 *
 * Drains the stream with `getReader()` rather than `for await…of`.
 * The async-iterator protocol on `ReadableStream` only landed in
 * Safari 17.4 (March 2024) and iOS Safari < 17.4 throws on the
 * iteration — the chatStore's catch then quietly marks the response
 * failed and the user sees a blank reply. `getReader()` has been in
 * every modern browser (including iOS Safari ≥ 14.5) for years, so
 * we use it directly and emulate the iterator's early-cancel
 * behavior in a `finally` block.
 *
 * @param byteStream The fetch response body to drain.
 * @param result Optional out-param; `sawDone` is set to `true` when
 *   the `[DONE]` sentinel is consumed. Callers distinguish a clean
 *   server close (`sawDone`) from a transport drop (stream ended /
 *   threw without `[DONE]`) to decide whether to reconnect.
 */
export async function* parseSseStream(
  byteStream: ReadableStream<Uint8Array>,
  result?: SseStreamResult,
): AsyncIterable<StreamEvent> {
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let currentEvent: string | null = null;
  const reader = byteStream.getReader();

  try {
    while (true) {
      // Sequential by design: each read waits for the next chunk
      // off the wire; chunks arrive serially. Promise.all does not
      // apply.
      // eslint-disable-next-line no-await-in-loop
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      while (buf.includes("\n")) {
        const idx = buf.indexOf("\n");
        let line = buf.slice(0, idx);
        buf = buf.slice(idx + 1);
        if (line.endsWith("\r")) line = line.slice(0, -1);

        if (line.startsWith("event: ")) {
          currentEvent = line.slice(7);
        } else if (line.startsWith("data: ")) {
          const dataStr = line.slice(6);
          if (dataStr.trim() === "[DONE]") {
            // The server's terminal sentinel is a bare `data: [DONE]` with
            // no preceding `event:` line (see `_stream_live_events`), so it
            // must be detected regardless of `currentEvent` — otherwise a
            // clean server close is indistinguishable from a transport drop.
            if (result) result.sawDone = true;
            return;
          }
          // A `data:` line is only a parseable event when an `event:` line
          // preceded it; a lone data line (other than [DONE]) is ignored.
          if (currentEvent !== null) {
            let data: Record<string, unknown>;
            try {
              data = JSON.parse(dataStr) as Record<string, unknown>;
            } catch {
              currentEvent = null;
              continue;
            }
            const event = parseEvent(currentEvent, data);
            if (event !== null) yield event;
            currentEvent = null;
          }
        } else if (line === "") {
          currentEvent = null;
        }
      }
    }
  } finally {
    // Emulate the async-iterator protocol's auto-cancel on early
    // termination: when a consumer breaks out (e.g. user hit Stop,
    // session switched), close the underlying connection rather
    // than leaving the fetch in flight until GC. cancel() also
    // releases the reader lock per spec, so no separate
    // releaseLock() call is needed.
    reader.cancel().catch(() => {});
  }
}

/**
 * Parse `{event, data}` JSONL lines into typed events. Convenience
 * for tests and offline replay — the wire-format streaming case uses
 * `parseSseStream` instead.
 */
export function* parseEventLines(lines: Iterable<string>): Iterable<StreamEvent> {
  for (const raw of lines) {
    const trimmed = raw.trim();
    if (!trimmed) continue;
    let parsed: { event?: string; data?: Record<string, unknown> };
    try {
      parsed = JSON.parse(trimmed) as typeof parsed;
    } catch {
      continue;
    }
    if (!parsed.event || !parsed.data) continue;
    const event = parseEvent(parsed.event, parsed.data);
    if (event !== null) yield event;
  }
}

/** Token/cost bucket keys on a `ModelUsage`, mapping wire (snake) to camel. */
const MODEL_USAGE_FIELDS: ReadonlyArray<{ wire: string; camel: keyof ModelUsage }> = [
  { wire: "input_tokens", camel: "inputTokens" },
  { wire: "output_tokens", camel: "outputTokens" },
  { wire: "total_tokens", camel: "totalTokens" },
  { wire: "cache_read_input_tokens", camel: "cacheReadInputTokens" },
  { wire: "cache_creation_input_tokens", camel: "cacheCreationInputTokens" },
  { wire: "total_cost_usd", camel: "totalCostUsd" },
];

/**
 * Parse the `usage_by_model` field of a `session.usage` event.
 *
 * Returns `undefined` when absent/null (the store keeps its cached map),
 * a `Record<string, ModelUsage>` when valid, or `null` when malformed —
 * which invalidates the whole event, matching the flat-field handling. The
 * server sends the complete merged per-model map when it changes, so a valid
 * value replaces the cached map wholesale. Each model's missing buckets
 * become `null` (the `ModelUsage` "not recorded" sentinel). A non-numeric or
 * negative bucket value, or a non-object entry, is treated as malformed.
 *
 * @param raw - The raw `usage_by_model` value off the event payload.
 */
function parseUsageByModel(raw: unknown): Record<string, ModelUsage> | undefined | null {
  if (raw === undefined || raw === null) return undefined;
  if (typeof raw !== "object" || Array.isArray(raw)) return null;
  const out: Record<string, ModelUsage> = {};
  for (const [model, entry] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) return null;
    const src = entry as Record<string, unknown>;
    const usage: ModelUsage = {
      inputTokens: null,
      outputTokens: null,
      totalTokens: null,
      cacheReadInputTokens: null,
      cacheCreationInputTokens: null,
      totalCostUsd: null,
    };
    for (const { wire, camel } of MODEL_USAGE_FIELDS) {
      const value = src[wire];
      if (value === undefined || value === null) continue;
      if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
      usage[camel] = value;
    }
    out[model] = usage;
  }
  return out;
}

/**
 * Normalize event type to handle a server-side enum-rendering quirk.
 *
 * The server builds terminal events as `f"response.{task.status}"` where
 * `task.status` may be a Python enum (rendering as
 * `response.TaskStatus.COMPLETED`) instead of the expected
 * `response.completed`. Normalize by extracting and lowercasing the
 * enum value.
 */
function normalizeEventType(eventType: string): string {
  if (eventType.includes(".TaskStatus.")) {
    const parts = eventType.split(".");
    const status = (parts[parts.length - 1] ?? "").toLowerCase();
    return `response.${status}`;
  }
  return eventType;
}

/**
 * Parse one raw SSE-shaped event payload (e.g. an entry from the
 * snapshot's `pending_elicitations` field) into a typed
 * `StreamEvent`. Exposed so cold-load paths can replay events
 * through the same reducer the live stream uses.
 *
 * @param rawType The event type string, e.g.
 *   `"response.elicitation_request"`.
 * @param data The raw `data` payload — the same JSON shape the
 *   server emits over SSE.
 * @returns A typed `StreamEvent`, or `null` for unknown types
 *   (forward-compatible — older clients ignore newer events).
 */
export function parseEvent(rawType: string, data: Record<string, unknown>): StreamEvent | null {
  const eventType = normalizeEventType(rawType);

  // Response lifecycle.
  if (eventType === "response.created") {
    return { type: "response_created", response: parseResponse(data) } satisfies ResponseCreated;
  }
  if (eventType === "response.queued") {
    return { type: "response_queued", response: parseResponse(data) } satisfies ResponseQueued;
  }
  if (eventType === "response.in_progress") {
    return {
      type: "response_in_progress",
      response: parseResponse(data),
    } satisfies ResponseInProgress;
  }
  if (eventType === "response.completed") {
    return {
      type: "response_completed",
      response: parseResponse(data),
    } satisfies ResponseCompleted;
  }
  if (eventType === "response.failed") {
    return { type: "response_failed", response: parseResponse(data) } satisfies ResponseFailed;
  }
  if (eventType === "response.incomplete") {
    const resp = parseResponse(data);
    return {
      type: "response_incomplete",
      response: resp,
      reason: resp.incompleteDetails?.reason ?? "",
    } satisfies ResponseIncomplete;
  }
  if (eventType === "response.cancelled") {
    return {
      type: "response_cancelled",
      response: parseResponse(data),
    } satisfies ResponseCancelled;
  }

  // Text streaming.
  if (eventType === "response.output_text.delta") {
    const delta = data.delta;
    if (typeof delta !== "string") return null;
    // Terminal-observed live streaming (claude-native) carries a stable
    // per-message id plus chunk ordering; ordinary task streaming omits
    // them. Pass through only well-typed values so a malformed field
    // can't poison the in-flight buffer.
    const messageId = typeof data.message_id === "string" ? data.message_id : undefined;
    const index = typeof data.index === "number" ? data.index : undefined;
    const final = typeof data.final === "boolean" ? data.final : undefined;
    return { type: "text_delta", delta, messageId, index, final } satisfies TextDelta;
  }

  // Reasoning.
  if (eventType === "response.reasoning.started") {
    return { type: "reasoning_started" } satisfies ReasoningStarted;
  }
  if (eventType === "response.reasoning_text.delta") {
    const delta = data.delta;
    if (typeof delta === "string")
      return { type: "reasoning_delta", delta } satisfies ReasoningDelta;
    return null;
  }
  if (eventType === "response.reasoning_summary_text.delta") {
    const delta = data.delta;
    if (typeof delta === "string")
      return { type: "reasoning_summary_delta", delta } satisfies ReasoningSummaryDelta;
    return null;
  }

  // Output items.
  if (eventType === "response.output_item.done") return parseOutputItem(data);

  // File output.
  if (eventType === "response.output_file.done") {
    return {
      type: "output_file_done",
      fileId: String(data.file_id ?? ""),
      filename: data.filename != null ? String(data.filename) : null,
      contentType: data.content_type != null ? String(data.content_type) : null,
    } satisfies OutputFileDone;
  }

  // Retry.
  if (eventType === "response.retry") {
    return {
      type: "retry",
      source: String(data.source ?? ""),
      toolName: data.tool_name != null ? String(data.tool_name) : null,
      attempt: Number(data.attempt ?? 0),
      maxAttempts: Number(data.max_attempts ?? 0),
      delaySeconds: Number(data.delay_seconds ?? 0),
      error: parseErrorInfo(data.error),
    } satisfies RetryEvent;
  }

  // Error.
  if (eventType === "response.error") {
    return {
      type: "error",
      source: String(data.source ?? ""),
      toolName: data.tool_name != null ? String(data.tool_name) : null,
      error: parseErrorInfo(data.error),
    } satisfies ErrorEvent;
  }

  // Compaction.
  if (eventType === "response.compaction.in_progress") {
    return { type: "compaction_in_progress" } satisfies CompactionInProgress;
  }
  if (eventType === "response.compaction.completed") {
    const tt = data.total_tokens;
    return {
      type: "compaction_completed",
      totalTokens: typeof tt === "number" ? tt : null,
    } satisfies CompactionCompleted;
  }
  if (eventType === "response.compaction.failed") {
    return { type: "compaction_failed" } satisfies CompactionFailed;
  }

  // Async client-tool cancel notification.
  if (eventType === "response.client_task.cancel") {
    const taskId = data.task_id;
    if (typeof taskId === "string" && taskId) {
      const rawCallId = data.call_id;
      const callId = typeof rawCallId === "string" && rawCallId ? rawCallId : null;
      return { type: "client_task_cancel", taskId, callId } satisfies ClientTaskCancel;
    }
    return null;
  }

  // Heartbeat — periodic keepalive, no payload. Explicit no-op so
  // it doesn't fall through the unknown-event branch and so a future
  // breaking rename surfaces here loudly.
  if (eventType === "response.heartbeat") {
    return null;
  }

  // Session lifecycle (session.*).
  if (eventType === "session.status") {
    const conversationId = data.conversation_id;
    const status = data.status;
    if (
      typeof conversationId === "string" &&
      conversationId &&
      (status === "idle" ||
        status === "launching" ||
        status === "running" ||
        status === "waiting" ||
        status === "failed")
    ) {
      const responseId = typeof data.response_id === "string" ? data.response_id : undefined;
      return {
        type: "session_status",
        conversationId,
        status,
        responseId,
      } satisfies SessionStatusEvent;
    }
    return null;
  }
  if (eventType === "session.usage") {
    const conversationId = data.conversation_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    const raw = data as Record<string, unknown>;
    const rawTokens = raw.context_tokens;
    const rawWindow = raw.context_window;
    // Validate present fields; absent fields fall through so the
    // store keeps the cached value.
    let contextTokens: number | undefined;
    if (rawTokens !== undefined && rawTokens !== null) {
      if (typeof rawTokens !== "number" || !Number.isFinite(rawTokens) || rawTokens < 0) {
        return null;
      }
      contextTokens = rawTokens;
    }
    let contextWindow: number | undefined;
    if (rawWindow !== undefined && rawWindow !== null) {
      if (typeof rawWindow !== "number" || !Number.isFinite(rawWindow) || rawWindow <= 0) {
        return null;
      }
      contextWindow = rawWindow;
    }
    // Cumulative session cost (USD). Present only on a priced session;
    // absent fields fall through so the store keeps the cached value.
    const rawCost = raw.total_cost_usd;
    let totalCostUsd: number | undefined;
    if (rawCost !== undefined && rawCost !== null) {
      if (typeof rawCost !== "number" || !Number.isFinite(rawCost) || rawCost < 0) {
        return null;
      }
      totalCostUsd = rawCost;
    }
    // Per-model breakdown (cumulative subtree map). The server sends the full
    // merged map when it changes, so a present value replaces the cached map
    // wholesale. Absent → undefined (keep cached); a malformed entry/bucket
    // invalidates the whole event.
    const usageByModel = parseUsageByModel(raw.usage_by_model);
    if (usageByModel === null) {
      return null;
    }
    if (
      contextTokens === undefined &&
      contextWindow === undefined &&
      totalCostUsd === undefined &&
      usageByModel === undefined
    ) {
      return null;
    }
    return {
      type: "session_usage",
      conversationId,
      ...(contextTokens !== undefined ? { contextTokens } : {}),
      ...(contextWindow !== undefined ? { contextWindow } : {}),
      ...(totalCostUsd !== undefined ? { totalCostUsd } : {}),
      ...(usageByModel !== undefined ? { usageByModel } : {}),
    } satisfies SessionUsageEvent;
  }
  if (eventType === "session.model") {
    const conversationId = data.conversation_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    const model = data.model;
    if (typeof model !== "string" || !model) return null;
    return { type: "session_model", conversationId, model } satisfies SessionModelEvent;
  }
  if (eventType === "session.reasoning_effort") {
    const conversationId = data.conversation_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    const reasoningEffort = data.reasoning_effort;
    if (reasoningEffort !== null && typeof reasoningEffort !== "string") return null;
    return {
      type: "session_reasoning_effort",
      conversationId,
      reasoningEffort,
    } satisfies SessionReasoningEffortEvent;
  }
  if (eventType === "session.collaboration_mode") {
    const conversationId = data.conversation_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    const mode = data.mode;
    if (typeof mode !== "string") return null;
    return {
      type: "session_collaboration_mode",
      conversationId,
      mode,
    } satisfies SessionCollaborationModeEvent;
  }
  if (eventType === "session.mode") {
    const conversationId = data.conversation_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    const mode = data.mode;
    if (typeof mode !== "string" || !mode) return null;
    return { type: "session_mode", conversationId, mode } satisfies SessionModeEvent;
  }
  if (eventType === "session.agent_changed") {
    const conversationId = data.conversation_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    const agentId = data.agent_id;
    if (typeof agentId !== "string" || !agentId) return null;
    const agentName = data.agent_name;
    if (typeof agentName !== "string" || !agentName) return null;
    return {
      type: "session_agent_changed",
      conversationId,
      agentId,
      agentName,
    } satisfies SessionAgentChangedEvent;
  }
  if (eventType === "session.todos") {
    const conversationId = data.conversation_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    const rawTodos = data.todos;
    if (!Array.isArray(rawTodos)) return null;
    // Validate and filter to well-formed todo items; silently drop
    // malformed entries so a bad item doesn't blank the whole panel.
    const todos = rawTodos.filter(
      (
        t,
      ): t is {
        content: string;
        status: "pending" | "in_progress" | "completed";
        activeForm: string;
      } =>
        t !== null &&
        typeof t === "object" &&
        typeof t.content === "string" &&
        (t.status === "pending" || t.status === "in_progress" || t.status === "completed") &&
        typeof t.activeForm === "string",
    );
    return {
      type: "session_todos",
      conversationId,
      todos,
    } satisfies SessionTodosEvent;
  }
  if (eventType === "session.terminal_pending") {
    const conversationId = data.conversation_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    // Coerce defensively: a malformed/absent `pending` is treated as
    // "not pending" so a bad frame can't strand the spinner on.
    const pending = data.pending === true;
    return {
      type: "session_terminal_pending",
      conversationId,
      pending,
    } satisfies SessionTerminalPendingEvent;
  }
  if (eventType === "session.sandbox_status") {
    const conversationId = data.conversation_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    const stage = data.stage;
    // Drop frames with an unknown stage rather than rendering a bogus
    // step — the snapshot re-seeds the indicator on the next load.
    if (
      stage !== "provisioning" &&
      stage !== "cloning" &&
      stage !== "starting" &&
      stage !== "connecting" &&
      stage !== "ready" &&
      stage !== "failed"
    ) {
      return null;
    }
    return {
      type: "session_sandbox_status",
      conversationId,
      stage,
      error: typeof data.error === "string" ? data.error : null,
    } satisfies SessionSandboxStatusEvent;
  }
  if (eventType === "session.input.consumed") {
    // Nested envelope: `{type, data: {item_id, type, data}}`.
    const inner = data.data;
    if (!inner || typeof inner !== "object" || Array.isArray(inner)) return null;
    const p = inner as Record<string, unknown>;
    const itemId = p.item_id;
    const itemType = p.type;
    const itemData = p.data;
    if (typeof itemId !== "string" || !itemId) return null;
    if (typeof itemType !== "string" || !itemType) return null;
    const payload =
      itemData && typeof itemData === "object" && !Array.isArray(itemData)
        ? (itemData as Record<string, unknown>)
        : {};
    // created_by is at the payload level (beside item_id/type), not in
    // the nested item data. Keep only a real string so null carries no author.
    const createdBy = p.created_by;
    const clearedPendingId = p.cleared_pending_id;
    return {
      type: "session_input_consumed",
      itemId,
      itemType,
      isMeta: payload.is_meta === true,
      ...(typeof createdBy === "string" ? { createdBy } : {}),
      data: payload,
      clearedPendingId: typeof clearedPendingId === "string" ? clearedPendingId : null,
    } satisfies SessionInputConsumedEvent;
  }
  if (eventType === "session.interrupted") {
    // Nested envelope: `{type, data: {requested_at}}`.
    const inner = data.data;
    if (!inner || typeof inner !== "object" || Array.isArray(inner)) return null;
    const p = inner as Record<string, unknown>;
    return {
      type: "session_interrupted",
      requestedAt: Number(p.requested_at ?? 0),
      responseId: typeof p.response_id === "string" ? p.response_id : undefined,
    } satisfies SessionInterruptedEvent;
  }
  if (eventType === "session.created") {
    const conversationId = data.conversation_id;
    const childSessionId = data.child_session_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    if (typeof childSessionId !== "string" || !childSessionId) return null;
    return {
      type: "session_created",
      conversationId,
      childSessionId,
      agentId: typeof data.agent_id === "string" ? data.agent_id : null,
      parentSessionId: typeof data.parent_session_id === "string" ? data.parent_session_id : null,
    } satisfies SessionCreatedEvent;
  }
  if (eventType === "session.resource.created") {
    const resource = parseSessionResource(data.resource);
    if (resource === null) return null;
    return { type: "session_resource_created", resource } satisfies SessionResourceCreatedEvent;
  }
  if (eventType === "session.resource.deleted") {
    const resourceId = data.resource_id;
    const resourceType = data.resource_type;
    const sessionId = data.session_id;
    if (typeof resourceId !== "string" || !resourceId) return null;
    if (typeof resourceType !== "string" || !resourceType) return null;
    if (typeof sessionId !== "string" || !sessionId) return null;
    return {
      type: "session_resource_deleted",
      resourceId,
      resourceType,
      sessionId,
    } satisfies SessionResourceDeletedEvent;
  }
  if (eventType === "session.child_session.updated") {
    const conversationId = data.conversation_id;
    const childSessionId = data.child_session_id;
    const child = data.child;
    if (typeof conversationId !== "string" || !conversationId) return null;
    if (typeof childSessionId !== "string" || !childSessionId) return null;
    if (!child || typeof child !== "object" || Array.isArray(child)) return null;
    return {
      type: "session_child_session_updated",
      conversationId,
      childSessionId,
      child: child as Record<string, unknown>,
    } satisfies SessionChildSessionUpdatedEvent;
  }
  if (eventType === "session.changed_files.invalidated") {
    const sessionId = data.session_id;
    if (typeof sessionId !== "string" || !sessionId) return null;
    return {
      type: "session_changed_files_invalidated",
      sessionId,
      environmentId: typeof data.environment_id === "string" ? data.environment_id : "default",
    } satisfies SessionChangedFilesInvalidatedEvent;
  }
  if (eventType === "session.terminal.activity") {
    const sessionId = data.session_id;
    const terminalId = data.terminal_id;
    if (typeof sessionId !== "string" || !sessionId) return null;
    if (typeof terminalId !== "string" || !terminalId) return null;
    return {
      type: "session_terminal_activity",
      sessionId,
      terminalId,
    } satisfies SessionTerminalActivityEvent;
  }
  if (eventType === "session.skills") {
    const conversationId = data.conversation_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    // Bare nudge — the runner's skills resolved. The store handler
    // refetches the (now-warm) snapshot and applies its `skills`.
    return {
      type: "session_skills",
      conversationId,
    } satisfies SessionSkillsEvent;
  }
  if (eventType === "session.model_options") {
    const conversationId = data.conversation_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    return {
      type: "session_model_options",
      conversationId,
    } satisfies SessionModelOptionsEvent;
  }
  if (eventType === "session.presence") {
    const conversationId = data.conversation_id;
    if (typeof conversationId !== "string" || !conversationId) return null;
    const rawViewers = data.viewers;
    if (!Array.isArray(rawViewers)) return null;
    const viewers: SessionViewer[] = [];
    for (const raw of rawViewers) {
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
      const entry = raw as Record<string, unknown>;
      const userId = entry.user_id;
      if (typeof userId !== "string" || !userId) return null;
      viewers.push({
        userId,
        ...(typeof entry.joined_at === "string" ? { joinedAt: entry.joined_at } : {}),
        idle: entry.idle === true,
      });
    }
    return {
      type: "session_presence",
      conversationId,
      viewers,
    } satisfies SessionPresenceEvent;
  }

  // MCP-shape elicitation request.
  if (eventType === "response.elicitation_request") {
    const elicitationId = data.elicitation_id;
    if (typeof elicitationId !== "string" || !elicitationId) return null;
    const params = data.params;
    if (!params || typeof params !== "object" || Array.isArray(params)) return null;
    const p = params as Record<string, unknown>;
    const requestedSchema = p.requestedSchema;
    const targetSessionId = p.target_session_id;
    const phase = String(p.phase ?? "");
    const policyName = String(p.policy_name ?? "");
    // The PermissionRequest endpoint stamps an `ask_user_question`
    // extra onto params when Claude is gating the built-in
    // AskUserQuestion tool. Surface it on the event so the UI's
    // ApprovalCard can render an interactive form WITHOUT having
    // to parse a (truncated) JSON-string content_preview.
    const askUserQuestionRaw = p.ask_user_question;
    // The PermissionRequest endpoint stamps an `exit_plan_mode`
    // extra (the FULL ExitPlanMode tool_input, untruncated) when
    // Claude is gating the built-in ExitPlanMode tool. Surface it
    // so the ApprovalCard can render the plan-review card.
    const exitPlanModeRaw = p.exit_plan_mode;
    const command = p.command;
    const cwd = p.cwd;
    const reason = p.reason;
    const execPolicyAmendment = p.execpolicy_amendment;
    // claude-native edit-tool prompts stamp this so the ApprovalCard
    // offers the "Accept & allow all edits" button (switches the
    // session to acceptEdits mode on accept).
    const allowAllEdits = p.allow_all_edits === true;
    return {
      type: "elicitation_request",
      elicitationId,
      targetSessionId:
        typeof targetSessionId === "string" && targetSessionId ? targetSessionId : null,
      message: String(p.message ?? ""),
      requestedSchema:
        requestedSchema && typeof requestedSchema === "object" && !Array.isArray(requestedSchema)
          ? (requestedSchema as Record<string, unknown>)
          : {},
      mode: String(p.mode ?? "form"),
      url: typeof p.url === "string" && p.url ? p.url : null,
      phase,
      policyName,
      contentPreview: String(p.content_preview ?? ""),
      askUserQuestion:
        askUserQuestionRaw &&
        typeof askUserQuestionRaw === "object" &&
        !Array.isArray(askUserQuestionRaw)
          ? (askUserQuestionRaw as Record<string, unknown>)
          : null,
      exitPlanMode:
        exitPlanModeRaw && typeof exitPlanModeRaw === "object" && !Array.isArray(exitPlanModeRaw)
          ? (exitPlanModeRaw as Record<string, unknown>)
          : null,
      codexCommand:
        phase === "codex_command_approval" && typeof command === "string" && command
          ? {
              command,
              cwd: typeof cwd === "string" && cwd ? cwd : null,
              reason: typeof reason === "string" && reason ? reason : null,
              execPolicyAmendment:
                Array.isArray(execPolicyAmendment) &&
                execPolicyAmendment.every((entry): entry is string => typeof entry === "string")
                  ? execPolicyAmendment
                  : null,
            }
          : null,
      allowAllEdits,
    } satisfies ElicitationRequest;
  }

  if (eventType === "response.elicitation_resolved") {
    const elicitationId = data.elicitation_id;
    if (typeof elicitationId !== "string" || !elicitationId) return null;
    return {
      type: "elicitation_resolved",
      elicitationId,
    } satisfies ElicitationResolved;
  }

  // Policy denied a user input or LLM call.
  if (eventType === "response.policy_denied") {
    return {
      type: "policy_denied",
      reason: String(data.reason ?? "Denied by policy"),
      phase: String(data.phase ?? ""),
    } satisfies PolicyDenied;
  }

  // Unknown event — skip gracefully for forward-compatibility.
  return null;
}

function parseSessionResource(raw: unknown): SessionResource | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const rec = raw as Record<string, unknown>;
  const id = rec.id;
  const type = rec.type;
  const name = rec.name;
  const sessionId = rec.session_id;
  const metadata = rec.metadata;
  if (typeof id !== "string" || !id) return null;
  if (typeof type !== "string" || !type) return null;
  if (typeof name !== "string") return null;
  // The owning session id is required to route the resource to the right
  // session's terminal cache; the runner always populates it on the wire.
  if (typeof sessionId !== "string" || !sessionId) return null;
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) return null;
  return {
    id,
    type,
    name,
    session_id: sessionId,
    metadata: metadata as Record<string, unknown>,
  };
}

function parseOutputItem(data: Record<string, unknown>): StreamEvent | null {
  const item = data.item;
  if (!item || typeof item !== "object" || Array.isArray(item)) return null;
  const rec = item as Record<string, unknown>;
  const itemType = String(rec.type ?? "");
  const itemId = String(rec.id ?? "");
  const responseId = String(rec.response_id ?? "");

  if (itemType === "function_call") {
    const argsStr = String(rec.arguments ?? "{}");
    let args: Record<string, unknown>;
    try {
      args = JSON.parse(argsStr) as Record<string, unknown>;
    } catch {
      args = {};
    }
    return {
      type: "tool_call",
      name: String(rec.name ?? ""),
      arguments: args,
      callId: String(rec.call_id ?? ""),
      status: String(rec.status ?? ""),
      agentName: String(rec.model ?? ""),
      itemId,
      responseId,
    } satisfies ToolCall;
  }

  if (itemType === "function_call_output") {
    return {
      type: "tool_result",
      callId: String(rec.call_id ?? ""),
      output: String(rec.output ?? ""),
      itemId,
      responseId,
    } satisfies ToolResult;
  }

  if (itemType === "message") {
    if (rec.is_meta === true) return null;
    const content = rec.content;
    return {
      type: "message_done",
      content: Array.isArray(content) ? (content as Array<Record<string, unknown>>) : [],
      itemId,
      responseId,
    } satisfies MessageDone;
  }

  if (itemType === "slash_command") {
    // Coerce a missing ``output`` (server-side exclude_none) to null
    // so downstream code branches on a single shape.
    const rawOutput = rec.output;
    const output = typeof rawOutput === "string" ? rawOutput : null;
    // Default to "skill" so server payloads predating the kind field
    // (older bridges, fixture replays) render as skills.
    const kind = rec.kind === "command" ? "command" : "skill";
    return {
      type: "slash_command",
      kind,
      name: String(rec.name ?? ""),
      arguments: String(rec.arguments ?? ""),
      output,
      agentName: String(rec.model ?? ""),
      // Present only on human-authored receipts (server exclude_none).
      ...(typeof rec.created_by === "string" ? { createdBy: rec.created_by } : {}),
      itemId,
      responseId,
    } satisfies SlashCommand;
  }

  if (itemType === "terminal_command") {
    return {
      type: "terminal_command",
      kind: rec.kind === "output" ? "output" : "input",
      input: typeof rec.input === "string" ? rec.input : null,
      stdout: typeof rec.stdout === "string" ? rec.stdout : null,
      stderr: typeof rec.stderr === "string" ? rec.stderr : null,
      itemId,
      responseId,
    } satisfies TerminalCommandEvent;
  }

  if (NATIVE_TOOL_TYPES.has(itemType)) {
    return {
      type: "native_tool_call",
      toolType: itemType,
      data: rec,
      itemId,
      responseId,
    } satisfies NativeToolCall;
  }

  // Compaction items, reasoning items, etc. — skip.
  return null;
}

function parseResponse(data: Record<string, unknown>): Response {
  // Some events put fields at the top level; others nest under `response`.
  const respData =
    data.response && typeof data.response === "object" && !Array.isArray(data.response)
      ? (data.response as Record<string, unknown>)
      : data;
  return responseFromJson(respData);
}

function responseFromJson(d: Record<string, unknown>): Response {
  const incRaw = d.incomplete_details;
  const incompleteDetails =
    incRaw && typeof incRaw === "object" && !Array.isArray(incRaw)
      ? { reason: String((incRaw as Record<string, unknown>).reason ?? "") }
      : null;
  const usageRaw = d.usage;
  const usage =
    usageRaw && typeof usageRaw === "object" && !Array.isArray(usageRaw)
      ? {
          inputTokens: Number((usageRaw as Record<string, unknown>).input_tokens ?? 0),
          outputTokens: Number((usageRaw as Record<string, unknown>).output_tokens ?? 0),
          // null when total_tokens absent (legacy responses pre-dating the fix
          // that computes input + output server-side).
          totalTokens:
            (usageRaw as Record<string, unknown>).total_tokens != null
              ? Number((usageRaw as Record<string, unknown>).total_tokens)
              : null,
          // Set only for multi-call turns (e.g. openai-agents with tool calls).
          // Prefer over totalTokens for context-ring display — totalTokens is
          // the billing sum which inflates on tool-call turns.
          contextTokens:
            (usageRaw as Record<string, unknown>).context_tokens != null
              ? Number((usageRaw as Record<string, unknown>).context_tokens)
              : null,
        }
      : null;
  const errRaw = d.error;
  const error =
    errRaw && typeof errRaw === "object" && !Array.isArray(errRaw) ? parseErrorInfo(errRaw) : null;
  const convRaw = d.conversation;
  const conversation =
    convRaw && typeof convRaw === "object" && !Array.isArray(convRaw)
      ? { id: String((convRaw as Record<string, unknown>).id ?? "") }
      : null;
  return {
    id: String(d.id ?? ""),
    status: String(d.status ?? ""),
    model: String(d.model ?? ""),
    output: Array.isArray(d.output) ? (d.output as Array<Record<string, unknown>>) : [],
    createdAt: Number(d.created_at ?? 0),
    completedAt: d.completed_at != null ? Number(d.completed_at) : null,
    previousResponseId: d.previous_response_id != null ? String(d.previous_response_id) : null,
    conversation,
    usage,
    error,
    incompleteDetails,
    background: Boolean(d.background ?? false),
    instructions: d.instructions != null ? String(d.instructions) : null,
  };
}

function parseErrorInfo(raw: unknown): ErrorInfo {
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    const r = raw as Record<string, unknown>;
    return { code: String(r.code ?? ""), message: String(r.message ?? "") };
  }
  return { code: "", message: String(raw ?? "") };
}
