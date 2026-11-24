// The gadget session: the Omnigent session that carries out a gadget's actions and edits. A gadget
// shown in a session's side panel sends them to that session; on the gadget's own page they go to
// the viewer's gadget session, the last session they showed it in. Requests are queued in the
// gadget's action ledger first, so the session can coordinate them and a new session can resume
// from the ledger.

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

/** Forgets every session this tab was about to start for a gadget; for tests. */
export function forgetGadgetSessionStarts(): void {
  pendingStarts.clear();
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

async function deliverMessage(
  appId: string,
  sessionId: string,
  message: string,
  actionId?: string,
): Promise<void> {
  await sendUserMessage(sessionId, message);
  if (actionId) {
    await recordDelivery(appId, actionId, sessionId);
  }
}

async function recordDelivery(appId: string, actionId: string, sessionId: string): Promise<void> {
  try {
    await recordGadgetActionDelivery(appId, actionId, sessionId);
  } catch {
    // The message reached the session; without the record its progress shows as not delivered.
  }
}

/**
 * Called when a session started from the gadget exists: shows the gadget in that session's side
 * panel, which also makes it the viewer's gadget session, and records as delivered there the
 * actions whose requests its first message carried. The viewer may have edited New session's
 * prompt, so an action whose request isn't in `firstMessage` stays undelivered.
 */
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
 * Counts as delivered the requests carried in `firstMessage`, the first message of the session
 * New session started for the gadget, once the server accepted that message: their actions
 * are recorded delivered and their `onDelivered` callbacks run (Address all marks its comments
 * addressed). Requests not in it stay queued.
 */
export async function acknowledgeStartedGadgetInput(
  appId: string,
  sessionId: string,
  firstMessage: string | null,
): Promise<void> {
  const started = pendingStarts.get(appId);
  if (started === undefined || Date.now() - started.at > PENDING_TTL_MS) return;
  pendingStarts.delete(appId);
  const carried = (message: string) => firstMessage !== null && firstMessage.includes(message);
  const sent = started.actionIds.filter((actionId) =>
    started.messages.some(
      (message) => message.includes(`"action_id":${JSON.stringify(actionId)}`) && carried(message),
    ),
  );
  await Promise.all(sent.map((actionId) => recordDelivery(appId, actionId, sessionId)));
  for (const { message, callback } of started.onDelivered) {
    if (carried(message)) callback(sessionId);
  }
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
  }: {
    deliver: boolean;
    actionId?: string;
    reveal?: boolean;
    railSessionId?: string;
    /**
     * For a request left in New session (`"starting"`): called once the session started there
     * got it in its first message. Never called when New session is left, or its prompt sent
     * without the request.
     */
    onDelivered?: (sessionId: string) => void;
  },
): Promise<"sent" | "starting"> {
  // Only the viewer's gadget session gets an action; a failed lookup sends nothing.
  let target = railSessionId;
  let show = reveal;
  let registered: string | null | undefined;
  if (target !== undefined && deliver && actionId !== undefined) {
    try {
      registered = await getGadgetGadgetSession(app.id);
    } catch (error) {
      throw new BridgeError(
        "UNAVAILABLE",
        `Couldn't find your gadget session: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    if (registered !== target) {
      target = undefined;
      show = false;
    }
  }
  if (target !== undefined) {
    if (deliver) {
      try {
        await deliverMessage(app.id, target, message, actionId);
      } catch (error) {
        throw new BridgeError(
          "UNAVAILABLE",
          `This session did not get the request: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    }
    return "sent";
  }
  const sessionId = registered !== undefined ? registered : await getGadgetGadgetSession(app.id);
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
        throw new BridgeError(
          "UNAVAILABLE",
          `Your gadget session did not get the request: ${error instanceof Error ? error.message : String(error)}`,
        );
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
): Promise<Record<string, unknown>> {
  if (!app.permissions.publish) {
    throw new BridgeError(
      "PERMISSION_DENIED",
      "Running actions on this gadget needs Can edit gadget access.",
    );
  }
  // Server refusals (an undeclared action, params that do not match) reach the frame with their code.
  const entry = await queueGadgetAction(app.id, {
    ...request,
    operationId: randomUUID(),
  });
  if (entry.delivered) {
    // The same request is still open in a session (or this is a retry of one that reached it),
    // so nothing is sent again.
    return { actionId: entry.id, delivery: entry.merged ? "merged" : "sent" };
  }
  // A new entry, or an open one that never reached a session (for example its New session was
  // abandoned): send its message, so the session that starts or gets it can carry it out.
  // The session works without the viewer watching; the page marks where it can be opened instead.
  const delivery = await openGadgetSession(app, gadgetActionMessage(entry), navigate, {
    deliver: true,
    actionId: entry.id,
    reveal: false,
    railSessionId,
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
