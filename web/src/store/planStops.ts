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
  /** The conversation item the stop message became, once known. */
  itemId?: string;
  /** A native session's pending input for the stop message, which names its item later. */
  pendingId?: string;
}

/** The session's receipt for a message: its item, or a native session's pending input. */
export interface MessageReceipt {
  itemId?: string;
  pendingId?: string;
}

/**
 * A send the composer shows before the agent takes it: `posted` once the
 * session accepted this page's POST, `replayed` when the server replays one it accepted.
 */
export interface PendingSend {
  tempId: string;
  content: MessageContentBlock[];
  posted?: boolean;
  replayed?: boolean;
  /** The server's pending input for this page's native send. */
  pendingId?: string;
}

function isAcceptedSend(send: PendingSend): boolean {
  return send.posted === true || send.replayed === true;
}

interface PlanStopsState {
  requests: Readonly<Partial<Record<string, PlanStopRequest>>>;
  /**
   * Per session, the item IDs and pending keys of transcript stop messages
   * that are over: the run was seen to stop after them, or the session took a
   * later request.
   */
  settledStops: Readonly<Partial<Record<string, readonly string[]>>>;
}

/**
 * Per session, the Stop the user asked of its run's main agent, kept until the
 * run stops, the message fails or the session takes another request. The
 * Tasks tab and the run rows all read it, so they agree the run is stopping.
 */
export const usePlanStops = create<PlanStopsState>(() => ({ requests: {}, settledStops: {} }));

// Only a session's latest stop messages matter; a few cover sends that cross.
const RECENT_KEPT = 8;

let nextRequestId = 1;
let nextObserverId = 1;
/** Per session, each surface watching it and whether that surface sees it working. */
const observers = new Map<string, Map<number, boolean>>();
/** Per session with a Stop to place, the items recent native receipts named, by pending input. */
const receiptItems = new Map<string, Map<string, string>>();
/** Per session, pending inputs of Stops a later request ended before their items were known. */
const supersededPendingStops = new Map<string, Set<string>>();
/** Per session, Stops a later request ended while their messages were still on the way. */
const supersededSendingStops = new Map<string, Set<number>>();

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
 * to stop the plan: the identities it goes by (its item ID and the pending key
 * it arrived under), null for another request, or undefined without any. A
 * send the session accepted but the agent hasn't taken yet is the latest; one
 * still on its way doesn't count.
 */
export function latestStopRequest(
  blocks: readonly AnyBlock[],
  pending: readonly PendingSend[],
): string[] | null | undefined {
  const latestPending = pending.findLast(isAcceptedSend);
  if (latestPending) {
    return isStopPlanMessage(messageText(latestPending.content)) ? [latestPending.tempId] : null;
  }
  const latest = blocks.findLast(isUserRequest);
  if (latest === undefined) return undefined;
  if (latest.type !== "user_message" || !isStopPlanMessage(messageText(latest.content))) {
    return null;
  }
  const identities = [latest.ctx.itemId, latest.stableKey].filter((id) => id != null);
  return identities.length > 0 ? identities : [""];
}

/**
 * Whether the latest request is a stop message whose Stop isn't over, so the
 * run reads as stopping while its session works.
 */
export function isOpenStopLatest(
  blocks: readonly AnyBlock[],
  pending: readonly PendingSend[],
  settled: readonly string[],
): boolean {
  const identities = latestStopRequest(blocks, pending);
  return Array.isArray(identities) && !identities.some((id) => settled.includes(id));
}

/** Whether the transcript shows another request from the user after the item *itemId*. */
export function hasRequestAfter(blocks: readonly AnyBlock[], itemId: string): boolean {
  const index = blocks.findLastIndex((block) => block.ctx.itemId === itemId);
  return index !== -1 && blocks.slice(index + 1).some(isUserRequest);
}

/**
 * The stop message a streamed transcript holds as its latest request: the
 * identities it goes by, and for a pending send, the server's pending input,
 * whose receipt names the item it becomes.
 */
interface TranscriptStop {
  identities: string[];
  pendingId?: string;
}

function transcriptStop(sessionId: string): TranscriptStop | null {
  const state = conversationRegistry.peek(sessionId)?.getState();
  if (!state) return null;
  const identities = latestStopRequest(state.blocks, state.pendingUserMessages);
  if (!Array.isArray(identities)) return null;
  const send = state.pendingUserMessages.findLast(isAcceptedSend);
  const pendingId = send && (send.pendingId ?? (send.replayed ? send.tempId : undefined));
  return pendingId ? { identities, pendingId } : { identities };
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
  if (!next) forgetReceiptsUnlessNeeded(sessionId);
}

function currentRequest(sessionId: string): PlanStopRequest | undefined {
  return usePlanStops.getState().requests[sessionId];
}

/** Whether a Stop of *sessionId*'s waits on a native receipt, to be placed or retired. */
function awaitsReceipts(sessionId: string): boolean {
  const request = currentRequest(sessionId);
  return (
    (request !== undefined && request.itemId === undefined) ||
    (supersededPendingStops.get(sessionId)?.size ?? 0) > 0 ||
    (supersededSendingStops.get(sessionId)?.size ?? 0) > 0
  );
}

/** Native receipts only matter while a Stop waits on one. */
function forgetReceiptsUnlessNeeded(sessionId: string): void {
  if (awaitsReceipts(sessionId)) return;
  receiptItems.delete(sessionId);
  supersededPendingStops.delete(sessionId);
  supersededSendingStops.delete(sessionId);
}

function retireStopMessage(sessionId: string, identities: readonly string[]): void {
  usePlanStops.setState((state) => {
    const settled = state.settledStops[sessionId] ?? [];
    const added = identities.filter((id) => !settled.includes(id));
    if (added.length === 0) return state;
    return {
      settledStops: {
        ...state.settledStops,
        [sessionId]: [...settled, ...added].slice(-RECENT_KEPT),
      },
    };
  });
}

/** Retire a transcript's stop message, and the item a pending one becomes once its receipt names it. */
function retireTranscriptStop(sessionId: string, stop: TranscriptStop | null): void {
  if (!stop) return;
  retireStopMessage(sessionId, stop.identities);
  if (stop.pendingId) retireOwnStopMessage(sessionId, { pendingId: stop.pendingId });
}

/** Retire a Stop's own message, now or once a native receipt names it. */
function retireOwnStopMessage(sessionId: string, receipt: MessageReceipt): void {
  const itemId =
    receipt.itemId ??
    (receipt.pendingId ? receiptItems.get(sessionId)?.get(receipt.pendingId) : undefined);
  if (itemId) retireStopMessage(sessionId, [itemId]);
  else if (receipt.pendingId) {
    const pending = supersededPendingStops.get(sessionId) ?? new Set<string>();
    supersededPendingStops.set(sessionId, pending.add(receipt.pendingId));
  }
}

/**
 * Show *sessionId*'s run as stopping while *send* delivers the Stop, then until
 * the run stops. A Stop the session doesn't take restores the Stop control.
 *
 * @param send Delivers the stop message, resolving to the session's receipt,
 *   or null when the session didn't take it.
 */
export async function trackPlanStop(
  sessionId: string,
  send: () => Promise<MessageReceipt | null>,
): Promise<void> {
  const sending: PlanStopRequest = { id: nextRequestId++, sent: false };
  replaceRequest(sessionId, currentRequest(sessionId), sending);
  let receipt: MessageReceipt | null = null;
  try {
    receipt = await send();
  } finally {
    const current = currentRequest(sessionId);
    if (supersededSendingStops.get(sessionId)?.delete(sending.id)) {
      if (receipt) retireOwnStopMessage(sessionId, receipt);
      forgetReceiptsUnlessNeeded(sessionId);
    } else if (current?.id === sending.id) {
      const itemId =
        receipt?.itemId ??
        (receipt?.pendingId ? receiptItems.get(sessionId)?.get(receipt.pendingId) : undefined);
      replaceRequest(
        sessionId,
        current,
        receipt
          ? {
              ...current,
              sent: true,
              ...(itemId ? { itemId } : {}),
              ...(receipt.pendingId ? { pendingId: receipt.pendingId } : {}),
            }
          : undefined,
      );
      if (itemId) forgetReceiptsUnlessNeeded(sessionId);
    }
  }
}

/**
 * A native session's receipt: its pending input *pendingId* became the item
 * *itemId*, which places a Stop's message in the transcript.
 */
export function notePlanStopReceipt(sessionId: string, pendingId: string, itemId: string): void {
  if (!awaitsReceipts(sessionId)) return;
  if (supersededPendingStops.get(sessionId)?.delete(pendingId)) {
    retireStopMessage(sessionId, [itemId]);
  }
  const request = currentRequest(sessionId);
  if (request?.pendingId === pendingId && request.itemId === undefined) {
    replaceRequest(sessionId, request, { ...request, itemId });
  } else {
    // The receipt may beat the delivery's response, which names the pending input.
    const items = receiptItems.get(sessionId) ?? new Map<string, string>();
    items.set(pendingId, itemId);
    for (const oldest of [...items.keys()].slice(0, -RECENT_KEPT)) items.delete(oldest);
    receiptItems.set(sessionId, items);
  }
  forgetReceiptsUnlessNeeded(sessionId);
}

/** End *request*, unless a later Stop took its place. */
export function endPlanStop(sessionId: string, request: PlanStopRequest): void {
  replaceRequest(sessionId, request, undefined);
}

/**
 * Note the Stop in progress, and the stop message the transcript shows, as the
 * user sends *sessionId* another message.
 *
 * @returns Ends both once the session takes the message, along with that
 *   Stop's own message when it arrives later. A Stop asked for after the
 *   message was sent isn't affected.
 */
export function supersedePlanStop(sessionId: string | null): () => void {
  if (sessionId === null) return () => undefined;
  const superseded = currentRequest(sessionId);
  const stopMessage = transcriptStop(sessionId);
  return () => {
    retireTranscriptStop(sessionId, stopMessage);
    const request = currentRequest(sessionId);
    if (superseded === undefined || request?.id !== superseded.id) return;
    if (request.sent) retireOwnStopMessage(sessionId, request);
    else {
      const sending = supersededSendingStops.get(sessionId) ?? new Set<number>();
      supersededSendingStops.set(sessionId, sending.add(request.id));
    }
    endPlanStop(sessionId, request);
  };
}

/**
 * Every surface watching the session sees it stopped: its sent Stop, if any,
 * is done, and so is the stop message the transcript last shows, so a later
 * wake isn't stopping. Surfaces count work differently, e.g. the Tasks tab
 * counts busy sub-agents, so a Stop settles only once none sees work.
 */
function settleIfStopped(sessionId: string): void {
  const watching = observers.get(sessionId);
  if (!watching || [...watching.values()].some(Boolean)) return;
  const request = currentRequest(sessionId);
  if (request?.sent) {
    retireOwnStopMessage(sessionId, request);
    endPlanStop(sessionId, request);
  }
  retireTranscriptStop(sessionId, transcriptStop(sessionId));
}

/** A new surface's ID for {@link watchPlanStop} and {@link reportPlanStopActivity}. */
export function newPlanStopObserver(): number {
  return nextObserverId++;
}

/**
 * Keep *sessionId*'s Stop while a surface watches the session. Once none does,
 * nothing would see the run stop, so the Stop is dropped; a transcript the page
 * streams stands in for it.
 *
 * @returns Stops watching.
 */
export function watchPlanStop(sessionId: string, observer: number): () => void {
  const watching = observers.get(sessionId) ?? new Map<number, boolean>();
  // A surface sees the session working until it says otherwise.
  watching.set(observer, true);
  observers.set(sessionId, watching);
  return () => {
    const current = observers.get(sessionId);
    if (!current?.delete(observer)) return;
    if (current.size > 0) {
      settleIfStopped(sessionId);
      return;
    }
    observers.delete(sessionId);
    replaceRequest(sessionId, currentRequest(sessionId), undefined);
  };
}

/** Whether *observer*, a surface watching *sessionId*, sees the session working. */
export function reportPlanStopActivity(
  sessionId: string,
  observer: number,
  working: boolean,
): void {
  const watching = observers.get(sessionId);
  if (!watching?.has(observer)) return;
  watching.set(observer, working);
  if (!working) settleIfStopped(sessionId);
}
