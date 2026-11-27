// Send action requests to the viewer's registered gadget session; use the ledger to resume
// delivery.

import { randomUUID } from "@/lib/randomUUID";
import { sendUserMessage } from "@/lib/sessionsApi";
import { queueGadgetComposerQuote } from "@/lib/gadgetComposerQuotes";
import {
  exitGadgetRailFullScreen,
  requestGadgetCreateSessionMenu,
  requestGadgetRailTab,
  setGadgetNewSessionTabMaximized,
  gadgetFillsScreen,
} from "@/lib/gadgetRailTabs";

import type { GadgetActionRequest, GadgetSelection } from "./gadgetBridge";
import { BridgeError } from "./gadgetBridge";
import { GADGET_SKILL_INSTRUCTION, gadgetPromptLabel } from "./gadgetBuild";
import {
  composeGadgetContextPrompt,
  gadgetBoardNewSessionUrl,
  gadgetSessionUrl,
} from "./gadgetPages";
import type { Gadget } from "../api/gadgetApi";
import {
  getGadgetGadgetSession,
  linkGadgetToSession,
  recordGadgetActionDelivery,
  requestGadgetAction as queueGadgetAction,
} from "../api/gadgetApi";

// A New session the viewer has not started within this long is not adopted as the gadget session.
const PENDING_TTL_MS = 30 * 60 * 1000;
// Gadgets whose gadget session this page opened New session for, when, and the actions its
// prompt carries. In memory: the pane reaches the created session without a reload, and after one
// the next request starts again.
const pendingStarts = new Map<
  string,
  {
    at: number;
    actionIds: string[];
    messages: string[];
    onDelivered: { message: string; callback: (sessionId: string) => void }[];
  }
>();
const SESSION_ID = /^[A-Za-z0-9_-]{1,128}$/;

type GadgetInfo = Pick<Gadget, "id" | "name" | "permissions">;

/** Opens a page in this tab; takes an app path from `gadgetPages` (the router's `navigate`). */
export type GadgetNavigate = (url: string) => void;

const RAIL_KEY_PREFIX = "gadget:";

/** The gadget's tab in a session's side panel. */
export function gadgetRailKey(appId: string): string {
  return `${RAIL_KEY_PREFIX}${appId}`;
}

export function gadgetIdFromRailKey(key: string): string | null {
  return key.startsWith(RAIL_KEY_PREFIX) ? key.slice(RAIL_KEY_PREFIX.length) : null;
}

// Gadgets whose gadget session is being created now: from the viewer's Create session until the
// session is linked and its first message counted, or the create fails. Requests wait for it, so
// the session isn't started twice and the gadget can show it is starting.
const STARTING_TIMEOUT_MS = 2 * 60 * 1000;
const startingSessions = new Map<string, ReturnType<typeof setTimeout>>();
const startingListeners = new Set<(appId: string, starting: boolean) => void>();

/** Marks the gadget's session as being created (`true`) or no longer (`false`). */
export function setGadgetSessionStarting(appId: string, starting: boolean): void {
  const timer = startingSessions.get(appId);
  if (starting === (timer !== undefined)) return;
  if (timer !== undefined) clearTimeout(timer);
  if (starting) {
    // A create that never reports back can't keep the gadget waiting.
    startingSessions.set(
      appId,
      setTimeout(() => setGadgetSessionStarting(appId, false), STARTING_TIMEOUT_MS),
    );
  } else {
    startingSessions.delete(appId);
  }
  for (const listener of [...startingListeners]) listener(appId, starting);
}

/** Whether the gadget's session is being created now. */
export function gadgetSessionStarting(appId: string): boolean {
  return startingSessions.has(appId);
}

/** Calls `listener` as gadgets' sessions start and finish starting. Returns an unsubscribe. */
export function onGadgetSessionStarting(
  listener: (appId: string, starting: boolean) => void,
): () => void {
  startingListeners.add(listener);
  return () => startingListeners.delete(listener);
}

type SessionGadgetsListener = (sessionId: string, apps: Gadget[]) => void;
const sessionGadgetsListeners = new Set<SessionGadgetsListener>();

/** Tells the side panel's cache about a session's gadgets after this module links one there. */
export function onSessionGadgetsChanged(listener: SessionGadgetsListener): () => void {
  sessionGadgetsListeners.add(listener);
  return () => sessionGadgetsListeners.delete(listener);
}

async function showGadgetInSession(
  appId: string,
  sessionId: string,
  options: { handlesActions?: boolean } = {},
): Promise<void> {
  const apps = await linkGadgetToSession(appId, sessionId, true, options);
  sessionGadgetsListeners.forEach((listener) => listener(sessionId, apps));
}

/**
 * Opens the session with the gadget shown in its side panel. A session the gadget only points
 * to (`handlesActions: false`, such as one its page cites) shows it without taking its actions.
 */
export async function openGadgetInSession(
  appId: string,
  sessionId: string,
  navigate: GadgetNavigate,
  options: { handlesActions?: boolean } = {},
): Promise<void> {
  // The side panel lists only the gadgets linked to the session, so link it first. A failed link
  // still opens the session; the gadget stays on its own page.
  await showGadgetInSession(appId, sessionId, options).catch(() => undefined);
  requestGadgetRailTab(sessionId, gadgetRailKey(appId));
  navigate(gadgetSessionUrl(sessionId));
}

/**
 * Opens the gadget filling the screen, with a session behind it: the viewer's gadget session, or
 * New session when they have none (or theirs is gone), which then gets the gadget once started.
 * A viewer who can edit starts it with the coordinator instructions in the composer. Opened from
 * the sidebar (`keepSidebar`), the sidebar stays open.
 */
export async function openGadgetFullScreen(
  app: GadgetInfo,
  navigate: GadgetNavigate,
  { keepSidebar = false }: { keepSidebar?: boolean } = {},
): Promise<void> {
  const sessionId = app.permissions.publish
    ? await getGadgetGadgetSession(app.id).catch(() => null)
    : null;
  if (sessionId !== null) {
    try {
      await showGadgetInSession(app.id, sessionId);
      requestGadgetRailTab(sessionId, gadgetRailKey(app.id), {
        maximize: true,
        keepSidebar,
      });
      navigate(gadgetSessionUrl(sessionId));
      return;
    } catch (error) {
      if (!sessionGone(error)) {
        throw error;
      }
    }
  }
  if (app.permissions.publish) {
    markPending(app.id);
  }
  setGadgetNewSessionTabMaximized(true);
  navigate(
    gadgetBoardNewSessionUrl(
      app.id,
      app.permissions.publish ? composeGadgetSessionPrompt(app) : "",
      {
        fullScreen: true,
      },
    ),
  );
}

/**
 * The note that makes a new session the gadget's coordinator, before its first request. How to
 * coordinate is in the `gadget` skill, so the note stays short.
 */
export function composeGadgetSessionPrompt(app: Pick<Gadget, "id" | "name">): string {
  return [
    GADGET_SKILL_INSTRUCTION,
    `This is my gadget session for ${gadgetPromptLabel(app)}: I'll send its actions and change requests here.`,
    "Start with any actions already queued in its ledger.",
  ].join(" ");
}

/** The message that hands one ledger entry to the gadget session. */
export function gadgetActionMessage(entry: {
  id: string;
  action: string;
  params: Record<string, unknown>;
}): string {
  return `Gadget action\n${JSON.stringify({ action_id: entry.id, action: entry.action, params: entry.params })}`;
}

/** Forgets every session this tab was about to start for a gadget, and kept deliveries; for tests. */
export function forgetGadgetSessionStarts(): void {
  pendingStarts.clear();
  unrecordedDeliveries.clear();
  for (const timer of startingSessions.values()) clearTimeout(timer);
  startingSessions.clear();
}

/** Remembers that this tab opened New session for the gadget's session, so it is adopted once started. */
export function markGadgetSessionPending(appId: string): void {
  markPending(appId);
}

function markPending(appId: string, actionId?: string, message?: string): void {
  pendingStarts.set(appId, {
    at: Date.now(),
    actionIds: actionId ? [actionId] : [],
    messages: message ? [message] : [],
    onDelivered: [],
  });
}

/**
 * Adds a request to the session the viewer is about to start for the gadget, so requests made
 * before it starts all go in its first message; returns that message's requests.
 */
function addPendingRequest(
  appId: string,
  message: string,
  actionId?: string,
  onDelivered?: (sessionId: string) => void,
): string[] {
  const current = pendingStarts.get(appId);
  const fresh = current !== undefined && Date.now() - current.at <= PENDING_TTL_MS;
  const actionIds = fresh ? current.actionIds : [];
  const messages = fresh ? current.messages : [];
  const delivered = fresh ? current.onDelivered : [];
  pendingStarts.set(appId, {
    at: Date.now(),
    actionIds: actionId && !actionIds.includes(actionId) ? [...actionIds, actionId] : actionIds,
    messages: messages.includes(message) ? messages : [...messages, message],
    onDelivered: onDelivered ? [...delivered, { message, callback: onDelivered }] : delivered,
  });
  return pendingStarts.get(appId)?.messages ?? [message];
}

/** A session's input policy refused the message: the agent never got it. */
class DeliveryRefused extends Error {}

async function deliverMessage(
  appId: string,
  sessionId: string,
  message: string,
  actionId?: string,
): Promise<void> {
  const result = await sendUserMessage(sessionId, message);
  // A refused message is answered like an accepted one, with `denied` set.
  if (result?.denied) throw new DeliveryRefused("A policy in that session refused the request.");
  if (actionId) {
    // The message reached the session, so it isn't sent again; a record that still fails is
    // kept and tried again instead.
    const failed = await recordGadgetDeliveries(appId, sessionId, [actionId]);
    keepUnrecordedDeliveries(appId, sessionId, failed);
  }
}

/**
 * A request that failed on its way to a session. `notSent` when it is known not to have
 * reached it: refused, or answered with a 4xx before it was taken. Otherwise (no answer, a
 * 5xx) the session may have it and act on it.
 */
export class DeliveryFailed extends BridgeError {
  readonly notSent: boolean;
  constructor(code: BridgeError["code"], message: string, notSent: boolean) {
    super(code, message);
    this.notSent = notSent;
  }
}

/** Whether a failed `openGadgetSession` may still have got its message to the session. */
export function mayHaveBeenDelivered(error: unknown): boolean {
  return !(error instanceof DeliveryFailed && error.notSent);
}

function deliveryError(target: string, error: unknown): DeliveryFailed {
  if (error instanceof DeliveryRefused) {
    return new DeliveryFailed("PERMISSION_DENIED", error.message, true);
  }
  const status = (error as { status?: unknown })?.status;
  const notSent = typeof status === "number" && status >= 400 && status < 500;
  const detail = error instanceof Error ? error.message : String(error);
  return new DeliveryFailed(
    "UNAVAILABLE",
    `${target} ${notSent ? "did not get" : "may not have got"} the request: ${detail}`,
    notSent,
  );
}

// Deliveries a session got whose records failed, by session, then gadget: their messages are
// not sent again, only their records are tried again.
const unrecordedDeliveries = new Map<string, Map<string, Set<string>>>();

/** Keeps `actionIds`, delivered to `sessionId` but not recorded, to record them later. */
export function keepUnrecordedDeliveries(
  appId: string,
  sessionId: string,
  actionIds: readonly string[],
): void {
  if (actionIds.length === 0) return;
  const byGadget = unrecordedDeliveries.get(sessionId) ?? new Map<string, Set<string>>();
  const kept = byGadget.get(appId) ?? new Set<string>();
  actionIds.forEach((actionId) => kept.add(actionId));
  byGadget.set(appId, kept);
  unrecordedDeliveries.set(sessionId, byGadget);
}

/** Tries again to record the deliveries kept for `sessionId`; ones still failing stay kept. */
export async function recordUnrecordedDeliveries(sessionId: string): Promise<void> {
  const byGadget = unrecordedDeliveries.get(sessionId);
  if (byGadget === undefined) return;
  unrecordedDeliveries.delete(sessionId);
  await Promise.all(
    [...byGadget].map(async ([appId, kept]) => {
      const actionIds = [...kept];
      const failed = await recordGadgetDeliveries(appId, sessionId, actionIds).catch(
        () => actionIds,
      );
      keepUnrecordedDeliveries(appId, sessionId, failed);
    }),
  );
}

/** The session that got `actionId`'s request although its delivery isn't recorded, if any. */
function unrecordedSession(appId: string, actionId: string): string | null {
  for (const [sessionId, byGadget] of unrecordedDeliveries) {
    if (byGadget.get(appId)?.has(actionId)) return sessionId;
  }
  return null;
}

/** Forgets the kept deliveries; for tests. */
export function forgetUnrecordedDeliveries(): void {
  unrecordedDeliveries.clear();
}

const RECORD_ATTEMPTS = 3;
const RECORD_RETRY_MS = 500;

/**
 * Records `actionIds` as delivered to `sessionId`, trying each a few times. Returns the ones
 * still not recorded, for the caller to try again later: the session has their requests, so
 * sending them again would repeat the work.
 */
export async function recordGadgetDeliveries(
  appId: string,
  sessionId: string,
  actionIds: readonly string[],
): Promise<string[]> {
  const recorded = await Promise.all(
    actionIds.map((actionId) => recordWithRetries(appId, actionId, sessionId, 0)),
  );
  return actionIds.filter((_actionId, index) => !recorded[index]);
}

async function recordWithRetries(
  appId: string,
  actionId: string,
  sessionId: string,
  attempt: number,
): Promise<boolean> {
  try {
    await recordGadgetActionDelivery(appId, actionId, sessionId);
    return true;
  } catch {
    if (attempt + 1 >= RECORD_ATTEMPTS) return false;
    await new Promise<void>((resolve) => {
      setTimeout(resolve, RECORD_RETRY_MS * 2 ** attempt);
    });
    return recordWithRetries(appId, actionId, sessionId, attempt + 1);
  }
}

/** Links a newly created session to the gadget; delivery acknowledgement is separate. */
export async function adoptStartedGadgetSession(
  appId: string,
  sessionId: string,
): Promise<boolean> {
  if (!isStartedSessionId(sessionId)) {
    return false; // A temporary ID while the session is created.
  }
  await showGadgetInSession(appId, sessionId);
  return true;
}

/**
 * Acknowledges only the requests carried by this session's accepted first input. Keeps other
 * requests queued; returns failed delivery IDs for retry.
 */
export async function acknowledgeStartedGadgetInput(
  appId: string,
  sessionId: string,
  acceptedText: string | null,
): Promise<string[]> {
  const started = pendingStarts.get(appId);
  if (started === undefined || Date.now() - started.at > PENDING_TTL_MS) return [];
  const carried = (message: string) => acceptedText !== null && acceptedText.includes(message);
  const actionMessage = (actionId: string) =>
    started.messages.find((message) => message.includes(`"action_id":${JSON.stringify(actionId)}`));
  const sent = started.actionIds.filter((actionId) => {
    const message = actionMessage(actionId);
    return message !== undefined && carried(message);
  });
  const left = {
    at: started.at,
    actionIds: started.actionIds.filter((actionId) => !sent.includes(actionId)),
    messages: started.messages.filter((message) => !carried(message)),
    onDelivered: started.onDelivered.filter(({ message }) => !carried(message)),
  };
  if (left.messages.length > 0) pendingStarts.set(appId, left);
  else pendingStarts.delete(appId);
  const failed = await recordGadgetDeliveries(appId, sessionId, sent);
  for (const { message, callback } of started.onDelivered) {
    if (carried(message)) callback(sessionId);
  }
  return failed;
}

/** Whether `sessionId` is a created session's ID, not the temporary one New session shows first. */
export function isStartedSessionId(sessionId: string): boolean {
  return SESSION_ID.test(sessionId);
}

function sessionGone(error: unknown): boolean {
  const status = (error as { status?: unknown })?.status;
  return status === 404 || status === 403 || status === 410;
}

/**
 * Delivers `message` to the session the gadget is shown beside (`railSessionId`), or else to the
 * viewer's gadget session, then opens that session with `reveal`. An action requested beside a
 * session that isn't the viewer's gadget session (one the gadget only cites) still goes to the
 * gadget session, without leaving the page. Opens New session with the
 * coordinator instructions followed by `message` when the viewer has no gadget session yet (or
 * theirs is gone): they have to start it there.
 */
export async function openGadgetSession(
  app: GadgetInfo,
  message: string,
  navigate: GadgetNavigate,
  {
    deliver,
    actionId,
    reveal = true,
    railSessionId,
    onDelivered,
    signal,
  }: {
    deliver: boolean;
    actionId?: string;
    reveal?: boolean;
    railSessionId?: string;
    /**
     * Cancels a call whose message hasn't left: checked after each lookup, before anything is
     * sent, queued as pending or navigated to.
     */
    signal?: AbortSignal;
    /**
     * For a request left in New session (`"starting"`): called once the session started there
     * got it in its first message. Never called when New session is left, or its prompt sent
     * without the request.
     */
    onDelivered?: (sessionId: string) => void;
  },
): Promise<"sent" | "starting"> {
  const stopIfCancelled = (): void => {
    if (signal?.aborted) throw new BridgeError("CANCELLED", "The call was cancelled.");
  };
  // Only the viewer's gadget session gets an action; a failed lookup sends nothing.
  let target = railSessionId;
  let show = reveal;
  let registered: string | null | undefined;
  if (target !== undefined && deliver && actionId !== undefined) {
    try {
      registered = await getGadgetGadgetSession(app.id);
    } catch (error) {
      throw new DeliveryFailed(
        "UNAVAILABLE",
        `Couldn't find your gadget session: ${error instanceof Error ? error.message : String(error)}`,
        true,
      );
    }
    if (registered !== target) {
      target = undefined;
      show = false;
    }
  }
  stopIfCancelled();
  if (target !== undefined) {
    if (deliver) {
      try {
        await deliverMessage(app.id, target, message, actionId);
      } catch (error) {
        throw deliveryError("This session", error);
      }
    }
    return "sent";
  }
  const sessionId = registered !== undefined ? registered : await getGadgetGadgetSession(app.id);
  stopIfCancelled();
  if (sessionId !== null) {
    try {
      if (deliver) {
        await deliverMessage(app.id, sessionId, message, actionId);
      }
      if (show) {
        await openGadgetInSession(app.id, sessionId, navigate);
      }
      return "sent";
    } catch (error) {
      if (!sessionGone(error)) {
        throw deliveryError("Your gadget session", error);
      }
    }
  }
  const requests = addPendingRequest(app.id, message, actionId, onDelivered);
  const prompt = [composeGadgetSessionPrompt(app), ...requests].join("\n\n");
  if (gadgetFillsScreen(app.id)) {
    // The gadget stays full screen: New session's composer holds the requests, and the Create
    // session menu beside the gadget starts it there.
    navigate(gadgetBoardNewSessionUrl(app.id, prompt, { fullScreen: true }));
    requestGadgetCreateSessionMenu();
    return "starting";
  }
  // New session's composer holds the request; its link leaves a docked gadget beside it.
  navigate(gadgetBoardNewSessionUrl(app.id, prompt));
  return "starting";
}

/** Queues an action requested by the gadget and hands it to the viewer's gadget session. */
export async function requestGadgetAction(
  app: GadgetInfo,
  request: GadgetActionRequest,
  navigate: GadgetNavigate,
  railSessionId?: string,
  signal?: AbortSignal,
): Promise<Record<string, unknown>> {
  if (!app.permissions.publish) {
    throw new BridgeError(
      "PERMISSION_DENIED",
      "Running actions on this gadget needs Can edit gadget access.",
    );
  }
  // The session being created carries the requests made before Create session; another one
  // now would miss it, and ask for a second session.
  if (gadgetSessionStarting(app.id)) {
    throw new BridgeError("UNAVAILABLE", "Your gadget session is starting. Try again once it has.");
  }
  // Server refusals (an undeclared action, params that do not match) reach the frame with their code.
  // The caller's key makes a retry (after a timeout, say) the same request, answered from the
  // first one even once it finished.
  const entry = await queueGadgetAction(app.id, {
    ...request,
    operationId: request.operationId ?? randomUUID(),
  });
  if (entry.delivered) {
    // The same request is still open in a session (or this is a retry of one that reached it),
    // so nothing is sent again.
    return { actionId: entry.id, delivery: entry.merged ? "merged" : "sent" };
  }
  const gotIt = unrecordedSession(app.id, entry.id);
  if (gotIt !== null) {
    // A session already got this request but its delivery wasn't recorded: record it, and
    // don't send the message again.
    await recordUnrecordedDeliveries(gotIt);
    return { actionId: entry.id, delivery: entry.merged ? "merged" : "sent" };
  }
  // Cancelled before its message left: it stays queued, and asking again with its key sends it.
  if (signal?.aborted) throw new BridgeError("CANCELLED", "The call was cancelled.");
  // A new entry, or an open one that never reached a session (for example its New session was
  // abandoned): send its message, so the session that starts or gets it can carry it out.
  // The session works without the viewer watching; the page marks where it can be opened instead.
  const delivery = await openGadgetSession(app, gadgetActionMessage(entry), navigate, {
    deliver: true,
    actionId: entry.id,
    reveal: false,
    railSessionId,
    signal,
  });
  return { actionId: entry.id, delivery };
}

/**
 * The quote a selection in the gadget becomes: the selected text, then where it is, so the agent
 * can find the exact record (`ref`) and tell repeated text apart (`occurrence`).
 */
export function gadgetSelectionQuote(
  app: Pick<Gadget, "id" | "name">,
  selection: GadgetSelection,
): string {
  const where = [`gadget_id ${app.id}`];
  if (selection.ref) {
    where.push(`ref ${selection.ref}`);
  }
  if (selection.occurrence > 1) {
    where.push(`occurrence ${selection.occurrence}`);
  }
  const subject = selection.label
    ? `${selection.label} in ${JSON.stringify(app.name)}`
    : JSON.stringify(app.name);
  return `${selection.text}\n— ${subject} (${where.join(", ")})`;
}

/**
 * Reply or Ask in side chat for a selection in the gadget: adds the quote to the composer of the
 * session the gadget is shown beside, or else of the viewer's gadget session, which then opens.
 * Without a gadget session, New session opens with the quote in its prompt; viewers who cannot
 * edit get a plain New session.
 */
export async function quoteInGadgetSession(
  app: GadgetInfo,
  kind: "reply" | "side_chat",
  quote: string,
  navigate: GadgetNavigate,
  railSessionId?: string,
): Promise<void> {
  if (railSessionId !== undefined) {
    queueGadgetComposerQuote(railSessionId, kind, quote);
    // The quote goes to the composer, which the gadget hides while it fills the screen.
    exitGadgetRailFullScreen(railSessionId);
    return;
  }
  const sessionId = app.permissions.publish ? await getGadgetGadgetSession(app.id) : null;
  if (sessionId !== null) {
    queueGadgetComposerQuote(sessionId, kind, quote);
    await openGadgetInSession(app.id, sessionId, navigate);
    return;
  }
  const quoted = `${quote
    .split("\n")
    .map((line) => `> ${line}`)
    .join("\n")}\n\n`;
  if (app.permissions.publish) {
    markPending(app.id);
    navigate(gadgetBoardNewSessionUrl(app.id, `${composeGadgetSessionPrompt(app)}\n\n${quoted}`));
    return;
  }
  navigate(gadgetBoardNewSessionUrl(app.id, composeGadgetContextPrompt(quoted, app)));
}
