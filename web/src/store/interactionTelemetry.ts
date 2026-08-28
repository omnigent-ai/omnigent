// Client interaction telemetry, DERIVED from the store's authoritative,
// reconnect-reconciled per-conversation state — not hooked onto raw SSE blocks.
//
// One subscriber owns the whole lifecycle for every live conversation:
//   - agent_run      — from `activeResponse.state` (streaming → terminal).
//   - tool_call      — from `tool_group` / `tool_result` blocks in the transcript.
//   - create_session — a brand-new session's "created → first AI activity",
//     registered by the create sites via `markSessionCreated` (they alone know
//     the host kind); completed on first assistant activity, failed if the first
//     turn ends without any.
//
// Why derive from state instead of hooking blocks: `blocks` and `activeResponse`
// are the committed, deduplicated, snapshot-reconciled truth the UI renders, so
// they survive reconnects and dropped stream frames (a lost `response_start` /
// `response_end` / tool block does not). And it keeps every emit in ONE testable
// place rather than scattered through the pump, where each un-handled block type
// (error, user-echo, …) was a fresh mis-attribution bug. `approval` stays a direct
// emit on the user action (chatStore.submitApproval) — it has no stream lifecycle.

import { startTimedInteraction, type TimedInteraction } from "@/lib/analyticsEmit";
import { conversationRegistry } from "./conversationRegistry";

type HostKind = "sandbox" | "computer";

interface ConvTracking {
  create?: { handle: TimedInteraction; settled: boolean };
  run?: { responseId: string; handle: TimedInteraction };
  tools: Map<string, TimedInteraction>;
  toolSeen: Set<string>;
  toolDone: Set<string>;
  // Ref of the last-scanned blocks array, so a state change that didn't touch
  // the transcript (e.g. an activeResponse-only update) skips the block scan.
  lastBlocks?: unknown;
}

const tracked = new Map<string, ConvTracking>();

/**
 * Drop all in-flight tracking without settling. Test-only: the projector is a
 * module singleton, so tests that drive shared state must reset it between cases
 * or one case's open spans leak into the next.
 */
export function resetInteractionTelemetryForTests(): void {
  tracked.clear();
}

function trackingFor(id: string): ConvTracking {
  let t = tracked.get(id);
  if (t === undefined) {
    t = { tools: new Map(), toolSeen: new Set(), toolDone: new Set() };
    tracked.set(id, t);
  }
  return t;
}

/**
 * Register a brand-new session for create_session timing. Called by the create
 * sites (the send/landing path and the New Chat dialog) — the only places that
 * know the host kind. Opens the span now; the subscriber completes it on the
 * session's first AI activity, or fails it if the first turn ends without any.
 * Idempotent per session id.
 */
export function markSessionCreated(sessionId: string, hostKind: HostKind): void {
  const t = trackingFor(sessionId);
  if (t.create !== undefined) return;
  t.create = {
    handle: startTimedInteraction(
      hostKind === "sandbox" ? "create_session_sandbox" : "create_session_computer",
      sessionId,
    ),
    settled: false,
  };
}

const TERMINAL_STATES = new Set(["completed", "failed", "cancelled", "incomplete"]);

function runStatus(state: string): "success" | "failure" | "cancelled" {
  if (state === "completed") return "success";
  if (state === "failed") return "failure";
  return "cancelled"; // cancelled / incomplete (interrupted)
}

// "First AI activity" = the first assistant output the user perceives: streamed
// text, streamed reasoning, or a tool call. NOT the `user_message` echo or an
// `error` block (those aren't assistant activity and must not complete the span).
function isActivityBlock(type: string): boolean {
  return type === "text_chunk" || type === "reasoning_chunk" || type === "tool_group";
}

function process(id: string): void {
  const entry = conversationRegistry.peek(id);
  if (entry === undefined || entry.disposed) {
    const existing = tracked.get(id);
    if (existing !== undefined) settleDead(id, existing);
    return;
  }
  const t = trackingFor(id);
  const state = entry.getState();
  const ar = state.activeResponse;

  // agent_run — from the active response lifecycle (at most one at a time).
  // `runSettled` records that a response for this session just ended (terminal or
  // superseded), which also bounds the create_session span below.
  let runSettled = false;
  if (ar !== null && ar.state === "streaming") {
    if (t.run === undefined || t.run.responseId !== ar.responseId) {
      if (t.run !== undefined) {
        t.run.handle.complete("cancelled"); // superseded before a terminal
        runSettled = true;
      }
      t.run = {
        responseId: ar.responseId,
        handle: startTimedInteraction("agent_run", ar.responseId),
      };
    }
  } else if (t.run !== undefined) {
    if (ar !== null && ar.responseId === t.run.responseId && TERMINAL_STATES.has(ar.state)) {
      t.run.handle.complete(runStatus(ar.state));
    } else {
      t.run.handle.complete("cancelled"); // cleared or replaced without a matching terminal
    }
    t.run = undefined;
    runSettled = true;
  }

  // tool_call — from the committed transcript. Re-scan only when it changed; the
  // seen/done sets make it emit once per callId across scans.
  if (state.blocks !== t.lastBlocks) {
    t.lastBlocks = state.blocks;
    for (const block of state.blocks) {
      if (block.type === "tool_group") {
        for (const ex of block.executions) {
          if (ex.callId && !t.toolSeen.has(ex.callId)) {
            t.toolSeen.add(ex.callId);
            t.tools.set(ex.callId, startTimedInteraction("tool_call", ex.callId, ex.name));
          }
        }
      } else if (block.type === "tool_result") {
        const open = t.tools.get(block.callId);
        if (open !== undefined && !t.toolDone.has(block.callId)) {
          t.toolDone.add(block.callId);
          t.tools.delete(block.callId);
          open.complete();
        }
      }
    }
  }

  // create_session — first assistant activity completes it. Otherwise it's failed
  // once its first response ends without any (`runSettled`, or a terminal
  // activeResponse) — bounding the span to the first turn so a *later* turn's
  // activity can never complete a stale span as an inflated success.
  const create = t.create;
  if (create !== undefined && !create.settled) {
    if (state.blocks.some((b) => isActivityBlock(b.type))) {
      create.handle.complete("success");
      create.settled = true;
    } else if (runSettled || (ar !== null && TERMINAL_STATES.has(ar.state))) {
      create.handle.fail();
      create.settled = true;
    }
  }
}

// A conversation that went away (evicted / released / switched-away-and-disposed)
// never delivers more state, so settle whatever it left open and drop tracking.
function settleDead(id: string, t: ConvTracking): void {
  if (t.run !== undefined) {
    t.run.handle.complete("cancelled");
    t.run = undefined;
  }
  for (const handle of t.tools.values()) handle.complete("cancelled");
  t.tools.clear();
  if (t.create !== undefined && !t.create.settled) {
    t.create.handle.fail();
    t.create.settled = true;
  }
  tracked.delete(id);
}

// Subscribe once. `subscribe` fires on every entry state change (with the changed
// id) but NOT on dispose, so after handling the change also sweep tracked
// conversations whose entry is now gone/disposed.
conversationRegistry.subscribe((id) => {
  try {
    process(id);
  } catch {
    // Telemetry must never break the app.
  }
  for (const [tid, t] of [...tracked]) {
    const entry = conversationRegistry.peek(tid);
    if (entry === undefined || entry.disposed) {
      try {
        settleDead(tid, t);
      } catch {
        // ignore
      }
    }
  }
});
