// The thingy session: the Omnigent session that carries out a thingy's actions and edits. A thingy
// shown in a session's side panel sends them to that session; on the thingy's own page they go to
// the viewer's thingy session, the last session they showed it in. Requests are queued in the
// thingy's action ledger first, so the session can coordinate them and a new session can resume
// from the ledger.

import { randomUUID } from "@/lib/randomUUID";
import { sendUserMessage } from "@/lib/sessionsApi";
import { queueThingyComposerQuote } from "@/lib/thingyComposerQuotes";
import {
  exitThingyRailFullScreen,
  requestThingyCreateSessionMenu,
  requestThingyRailTab,
  setThingyNewSessionTabMaximized,
  thingyFillsScreen,
} from "@/lib/thingyRailTabs";

import type { ThingyActionRequest, ThingySelection } from "./thingyBridge";
import { BridgeError } from "./thingyBridge";
import { THINGY_SKILL_INSTRUCTION, thingyPromptLabel } from "./thingyBuild";
import {
  composeThingyContextPrompt,
  thingyBoardNewSessionUrl,
  thingySessionUrl,
} from "./thingyPages";
import type { Thingy } from "../api/thingyApi";
import {
  getThingyThingySession,
  linkThingyToSession,
  recordThingyActionDelivery,
  requestThingyAction as queueThingyAction,
} from "../api/thingyApi";

// A New session the viewer has not started within this long is not adopted as the thingy session.
const PENDING_TTL_MS = 30 * 60 * 1000;
// Thingies whose thingy session this page opened New session for, when, and the actions its
// prompt carries. In memory: the pane reaches the created session without a reload, and after one
// the next request starts again.
const pendingStarts = new Map<string, { at: number; actionIds: string[]; messages: string[] }>();
const SESSION_ID = /^[A-Za-z0-9_-]{1,128}$/;

type ThingyInfo = Pick<Thingy, "id" | "name" | "permissions">;

/** Opens a page in this tab; takes an app path from `thingyPages` (the router's `navigate`). */
export type ThingyNavigate = (url: string) => void;

const RAIL_KEY_PREFIX = "thingy:";

/** The thingy's tab in a session's side panel. */
export function thingyRailKey(appId: string): string {
  return `${RAIL_KEY_PREFIX}${appId}`;
}

export function thingyIdFromRailKey(key: string): string | null {
  return key.startsWith(RAIL_KEY_PREFIX) ? key.slice(RAIL_KEY_PREFIX.length) : null;
}

type SessionThingiesListener = (sessionId: string, apps: Thingy[]) => void;
const sessionThingiesListeners = new Set<SessionThingiesListener>();

/** Tells the side panel's cache about a session's thingies after this module links one there. */
export function onSessionThingiesChanged(listener: SessionThingiesListener): () => void {
  sessionThingiesListeners.add(listener);
  return () => sessionThingiesListeners.delete(listener);
}

async function showThingyInSession(appId: string, sessionId: string): Promise<void> {
  const apps = await linkThingyToSession(appId, sessionId, true);
  sessionThingiesListeners.forEach((listener) => listener(sessionId, apps));
}

/** Opens the session with the thingy shown in its side panel. */
export async function openThingyInSession(
  appId: string,
  sessionId: string,
  navigate: ThingyNavigate,
): Promise<void> {
  // The side panel lists only the thingies linked to the session, so link it first. A failed link
  // still opens the session; the thingy stays on its own page.
  await showThingyInSession(appId, sessionId).catch(() => undefined);
  requestThingyRailTab(sessionId, thingyRailKey(appId));
  navigate(thingySessionUrl(sessionId));
}

/**
 * Opens the thingy filling the screen, with a session behind it: the viewer's thingy session, or
 * New session when they have none (or theirs is gone), which then gets the thingy once started.
 * A viewer who can edit starts it with the coordinator instructions in the composer. Opened from
 * the sidebar (`keepSidebar`), the sidebar stays open.
 */
export async function openThingyFullScreen(
  app: ThingyInfo,
  navigate: ThingyNavigate,
  { keepSidebar = false }: { keepSidebar?: boolean } = {},
): Promise<void> {
  const sessionId = app.permissions.publish
    ? await getThingyThingySession(app.id).catch(() => null)
    : null;
  if (sessionId !== null) {
    try {
      await showThingyInSession(app.id, sessionId);
      requestThingyRailTab(sessionId, thingyRailKey(app.id), {
        maximize: true,
        keepSidebar,
      });
      navigate(thingySessionUrl(sessionId));
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
  setThingyNewSessionTabMaximized(true);
  navigate(
    thingyBoardNewSessionUrl(
      app.id,
      app.permissions.publish ? composeThingySessionPrompt(app) : "",
      {
        fullScreen: true,
      },
    ),
  );
}

/**
 * The note that makes a new session the thingy's coordinator, before its first request. How to
 * coordinate is in the `thingy` skill, so the note stays short.
 */
export function composeThingySessionPrompt(app: Pick<Thingy, "id" | "name">): string {
  return [
    THINGY_SKILL_INSTRUCTION,
    `This is my thingy session for ${thingyPromptLabel(app)}: I'll send its actions and change requests here.`,
    "Start with any actions already queued in its ledger.",
  ].join(" ");
}

/** The message that hands one ledger entry to the thingy session. */
export function thingyActionMessage(entry: {
  id: string;
  action: string;
  params: Record<string, unknown>;
}): string {
  return `Thingy action\n${JSON.stringify({ action_id: entry.id, action: entry.action, params: entry.params })}`;
}

/** Forgets every session this tab was about to start for a thingy; for tests. */
export function forgetThingySessionStarts(): void {
  pendingStarts.clear();
}

/** Remembers that this tab opened New session for the thingy's session, so it is adopted once started. */
export function markThingySessionPending(appId: string): void {
  markPending(appId);
}

function markPending(appId: string, actionId?: string, message?: string): void {
  pendingStarts.set(appId, {
    at: Date.now(),
    actionIds: actionId ? [actionId] : [],
    messages: message ? [message] : [],
  });
}

/**
 * Adds a request to the session the viewer is about to start for the thingy, so requests made
 * before it starts all go in its first message; returns that message's requests.
 */
function addPendingRequest(appId: string, message: string, actionId?: string): string[] {
  const current = pendingStarts.get(appId);
  const fresh = current !== undefined && Date.now() - current.at <= PENDING_TTL_MS;
  const actionIds = fresh ? current.actionIds : [];
  const messages = fresh ? current.messages : [];
  pendingStarts.set(appId, {
    at: Date.now(),
    actionIds: actionId && !actionIds.includes(actionId) ? [...actionIds, actionId] : actionIds,
    messages: messages.includes(message) ? messages : [...messages, message],
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
    await recordThingyActionDelivery(appId, actionId, sessionId);
  } catch {
    // The message reached the session; without the record its progress shows as not delivered.
  }
}

/**
 * Called when a session started from the thingy exists: shows the thingy in that session's side
 * panel, which also makes it the viewer's thingy session, and records the actions its prompt
 * carried as delivered there.
 */
export async function adoptStartedThingySession(
  appId: string,
  sessionId: string,
): Promise<boolean> {
  if (!isStartedSessionId(sessionId)) {
    return false; // A temporary ID while the session is created.
  }
  await showThingyInSession(appId, sessionId);
  const started = pendingStarts.get(appId);
  if (started !== undefined && Date.now() - started.at <= PENDING_TTL_MS) {
    pendingStarts.delete(appId);
    await Promise.all(
      started.actionIds.map((actionId) => recordDelivery(appId, actionId, sessionId)),
    );
  }
  return true;
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
 * Delivers `message` to the session the thingy is shown beside (`railSessionId`), or else to the
 * viewer's thingy session, then opens that session with `reveal`. Opens New session with the
 * coordinator instructions followed by `message` when the viewer has no thingy session yet (or
 * theirs is gone): they have to start it there.
 */
export async function openThingySession(
  app: ThingyInfo,
  message: string,
  navigate: ThingyNavigate,
  {
    deliver,
    actionId,
    reveal = true,
    railSessionId,
  }: { deliver: boolean; actionId?: string; reveal?: boolean; railSessionId?: string },
): Promise<"sent" | "starting"> {
  if (railSessionId !== undefined) {
    if (deliver) {
      try {
        await deliverMessage(app.id, railSessionId, message, actionId);
      } catch (error) {
        throw new BridgeError(
          "UNAVAILABLE",
          `This session did not get the request: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    }
    return "sent";
  }
  const sessionId = await getThingyThingySession(app.id);
  if (sessionId !== null) {
    try {
      if (deliver) {
        await deliverMessage(app.id, sessionId, message, actionId);
      }
      if (reveal) {
        await openThingyInSession(app.id, sessionId, navigate);
      }
      return "sent";
    } catch (error) {
      if (!sessionGone(error)) {
        throw new BridgeError(
          "UNAVAILABLE",
          `Your thingy session did not get the request: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    }
  }
  const requests = addPendingRequest(app.id, message, actionId);
  const prompt = [composeThingySessionPrompt(app), ...requests].join("\n\n");
  if (thingyFillsScreen(app.id)) {
    // The thingy stays full screen: New session's composer holds the requests, and the Create
    // session menu beside the thingy starts it there.
    navigate(thingyBoardNewSessionUrl(app.id, prompt, { fullScreen: true }));
    requestThingyCreateSessionMenu();
    return "starting";
  }
  // New session's composer holds the request; its link leaves a docked thingy beside it.
  navigate(thingyBoardNewSessionUrl(app.id, prompt));
  return "starting";
}

/** Queues an action requested by the thingy and hands it to the viewer's thingy session. */
export async function requestThingyAction(
  app: ThingyInfo,
  request: ThingyActionRequest,
  navigate: ThingyNavigate,
  railSessionId?: string,
): Promise<Record<string, unknown>> {
  if (!app.permissions.publish) {
    throw new BridgeError(
      "PERMISSION_DENIED",
      "Running actions on this thingy needs Can edit thingy access.",
    );
  }
  // Server refusals (an undeclared action, params that do not match) reach the frame with their code.
  const entry = await queueThingyAction(app.id, {
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
  const delivery = await openThingySession(app, thingyActionMessage(entry), navigate, {
    deliver: true,
    actionId: entry.id,
    reveal: false,
    railSessionId,
  });
  return { actionId: entry.id, delivery };
}

/**
 * The quote a selection in the thingy becomes: the selected text, then where it is, so the agent
 * can find the exact record (`ref`) and tell repeated text apart (`occurrence`).
 */
export function thingySelectionQuote(
  app: Pick<Thingy, "id" | "name">,
  selection: ThingySelection,
): string {
  const where = [`thingy_id ${app.id}`];
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
 * Reply or Ask in side chat for a selection in the thingy: adds the quote to the composer of the
 * session the thingy is shown beside, or else of the viewer's thingy session, which then opens.
 * Without a thingy session, New session opens with the quote in its prompt; viewers who cannot
 * edit get a plain New session.
 */
export async function quoteInThingySession(
  app: ThingyInfo,
  kind: "reply" | "side_chat",
  quote: string,
  navigate: ThingyNavigate,
  railSessionId?: string,
): Promise<void> {
  if (railSessionId !== undefined) {
    queueThingyComposerQuote(railSessionId, kind, quote);
    // The quote goes to the composer, which the thingy hides while it fills the screen.
    exitThingyRailFullScreen(railSessionId);
    return;
  }
  const sessionId = app.permissions.publish ? await getThingyThingySession(app.id) : null;
  if (sessionId !== null) {
    queueThingyComposerQuote(sessionId, kind, quote);
    await openThingyInSession(app.id, sessionId, navigate);
    return;
  }
  const quoted = `${quote
    .split("\n")
    .map((line) => `> ${line}`)
    .join("\n")}\n\n`;
  if (app.permissions.publish) {
    markPending(app.id);
    navigate(thingyBoardNewSessionUrl(app.id, `${composeThingySessionPrompt(app)}\n\n${quoted}`));
    return;
  }
  navigate(thingyBoardNewSessionUrl(app.id, composeThingyContextPrompt(quoted, app)));
}
