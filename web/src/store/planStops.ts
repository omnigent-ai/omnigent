import { create } from "zustand";

import { type AnyBlock, isTextBlock, type MessageContentBlock } from "@/lib/blocks";
import { splitSessionMessageOrigin } from "@/lib/sessionMessageOrigin";
import { isSystemUserContent } from "@/lib/systemMessage";
import { isStopPlanMessage } from "@/lib/workflow";
import { conversationRegistry } from "@/store/conversationRegistry";

/** A Stop the user asked of a run's main agent; `sent` once its session took the message. */
export interface PlanStopRequest {
  /** Tells this Stop from a later one, across its updates. */
  id: number;
  sent: boolean;
  /** The stop message the transcript last showed when this Stop began, which isn't this Stop's. */
  earlierStop: string | null;
  /** Whether the transcript has since shown a newer stop message as the latest request. */
  seen?: boolean;
}

/** A send the composer shows before the agent takes it; `posted` once the session accepted it. */
export interface PendingSend {
  tempId: string;
  content: MessageContentBlock[];
  posted?: boolean;
}

interface PlanStopsState {
  requests: Readonly<Partial<Record<string, PlanStopRequest>>>;
  /**
   * Per session, transcript stop messages that are over: the run was seen to
   * stop after them, or the session took a later request.
   */
  settledStops: Readonly<Partial<Record<string, readonly string[]>>>;
}

/**
 * Per session, the Stop the user asked of its run's main agent, kept until the
 * run stops, the message fails or the session takes another request. The
 * Tasks tab and the run rows all read it, so they agree the run is stopping.
 */
export const usePlanStops = create<PlanStopsState>(() => ({ requests: {}, settledStops: {} }));

// Only a session's latest stop message can stand in for a Stop; a few cover sends that cross.
const SETTLED_STOPS_KEPT = 8;

let nextRequestId = 1;
const watcherCounts = new Map<string, number>();

function messageText(content: readonly MessageContentBlock[]): string {
  return content
    .filter(isTextBlock)
    .map((block) => block.text)
    .join("");
}

/**
 * A message or command the user sent the session, not a runtime notice or
 * another session's message, such as a worker reporting back.
 */
function isUserRequest(block: AnyBlock): boolean {
  return (
    block.type === "slash_command" ||
    (block.type === "user_message" &&
      !isSystemUserContent(block.content) &&
      splitSessionMessageOrigin(messageText(block.content)) === null)
  );
}

/**
 * The latest request the user sent the session, when it asks its main agent
 * to stop the plan: its key, null for another request, or undefined without
 * any. A send the session accepted but the agent hasn't taken yet is the
 * latest; one still on its way doesn't count.
 */
export function latestStopRequest(
  blocks: readonly AnyBlock[],
  pending: readonly PendingSend[],
): string | null | undefined {
  const latestPending = pending.findLast((send) => send.posted === true);
  if (latestPending) {
    return isStopPlanMessage(messageText(latestPending.content)) ? latestPending.tempId : null;
  }
  const latest = blocks.findLast(isUserRequest);
  if (latest === undefined) return undefined;
  if (latest.type !== "user_message" || !isStopPlanMessage(messageText(latest.content))) {
    return null;
  }
  // A send the agent took keeps its pending key, so it stays the same message.
  return latest.stableKey ?? latest.ctx.itemId ?? "";
}

/** The stop message the streamed transcript holds as its latest request, if any. */
function committedStopMessage(sessionId: string): string | null {
  const entry = conversationRegistry.peek(sessionId);
  const latest = entry ? latestStopRequest(entry.getState().blocks, []) : null;
  return typeof latest === "string" ? latest : null;
}

/** Replace *sessionId*'s request, unless a later Stop already took the place of *current*. */
function replaceRequest(
  sessionId: string,
  current: PlanStopRequest | undefined,
  next: PlanStopRequest | undefined,
): void {
  usePlanStops.setState((state) => {
    if (state.requests[sessionId]?.id !== current?.id || current === next) return state;
    const others = Object.entries(state.requests).filter(([id]) => id !== sessionId);
    return { requests: Object.fromEntries(next ? [...others, [sessionId, next]] : others) };
  });
}

function currentRequest(sessionId: string): PlanStopRequest | undefined {
  return usePlanStops.getState().requests[sessionId];
}

function retireStopMessage(sessionId: string, stopMessage: string): void {
  usePlanStops.setState((state) => {
    const settled = state.settledStops[sessionId] ?? [];
    if (settled.includes(stopMessage)) return state;
    return {
      settledStops: {
        ...state.settledStops,
        [sessionId]: [...settled, stopMessage].slice(-SETTLED_STOPS_KEPT),
      },
    };
  });
}

/**
 * Show *sessionId*'s run as stopping while *send* delivers the Stop, then until
 * the run stops. A Stop the session doesn't take restores the Stop control.
 *
 * @param send Delivers the stop message, resolving to whether the session took it.
 */
export async function trackPlanStop(
  sessionId: string,
  send: () => Promise<boolean>,
): Promise<void> {
  const sending: PlanStopRequest = {
    id: nextRequestId++,
    sent: false,
    earlierStop: committedStopMessage(sessionId),
  };
  replaceRequest(sessionId, currentRequest(sessionId), sending);
  let taken = false;
  try {
    taken = await send();
  } finally {
    const current = currentRequest(sessionId);
    if (current?.id === sending.id) {
      replaceRequest(sessionId, current, taken ? { ...current, sent: true } : undefined);
    }
  }
}

/** End *request*, unless a later Stop took its place. */
export function endPlanStop(sessionId: string, request: PlanStopRequest): void {
  replaceRequest(sessionId, request, undefined);
}

/** The transcript shows a stop message newer than *request* began with, so a later request ends it. */
export function notePlanStopSeen(sessionId: string, request: PlanStopRequest): void {
  if (!request.seen) replaceRequest(sessionId, request, { ...request, seen: true });
}

/**
 * Note the Stop in progress, and the stop message the transcript shows, as the
 * user sends *sessionId* another message.
 *
 * @returns Ends both once the session takes the message. A Stop asked for
 *   after the message was sent isn't affected.
 */
export function supersedePlanStop(sessionId: string | null): () => void {
  if (sessionId === null) return () => undefined;
  const superseded = currentRequest(sessionId);
  const stopMessage = committedStopMessage(sessionId);
  return () => {
    if (superseded) endPlanStop(sessionId, superseded);
    if (stopMessage !== null) retireStopMessage(sessionId, stopMessage);
  };
}

/**
 * The session stopped working: its sent Stop, if any, is done, and so is the
 * stop message the transcript last shows, so a later wake isn't stopping.
 *
 * @param stopMessage The key of the transcript's latest stop message, or null.
 */
export function settlePlanStop(
  sessionId: string,
  request: PlanStopRequest | undefined,
  stopMessage: string | null,
): void {
  if (request?.sent) endPlanStop(sessionId, request);
  if (stopMessage !== null) retireStopMessage(sessionId, stopMessage);
}

/**
 * Keep *sessionId*'s Stop while a surface watches the session. Once none does,
 * nothing would see the run stop, so the Stop is dropped; a transcript the page
 * streams stands in for it.
 *
 * @returns Stops watching.
 */
export function watchPlanStop(sessionId: string): () => void {
  watcherCounts.set(sessionId, (watcherCounts.get(sessionId) ?? 0) + 1);
  return () => {
    const remaining = (watcherCounts.get(sessionId) ?? 1) - 1;
    if (remaining > 0) {
      watcherCounts.set(sessionId, remaining);
      return;
    }
    watcherCounts.delete(sessionId);
    replaceRequest(sessionId, currentRequest(sessionId), undefined);
  };
}
