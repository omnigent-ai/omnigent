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
  requestThingyRailTab,
  setThingyNewSessionTabMaximized,
} from "@/lib/thingyRailTabs";

import type { ThingyActionRequest, ThingySelection } from "./thingyBridge";
import { BridgeError } from "./thingyBridge";
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
const pendingStarts = new Map<string, { at: number; actionIds: string[] }>();
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
    ),
  );
}

/** The instructions that make a new session the thingy's coordinator, before its first request. */
export function composeThingySessionPrompt(app: Pick<Thingy, "id" | "name">): string {
  return [
    `This is my thingy session for the thingy ${JSON.stringify(app.name)} (thingy_id ${app.id}). I will send every action and change request for this thingy here.`,
    "- Start by calling sys_thingy_describe (its guide, actions and rules) and sys_thingy_action_list (its action ledger).",
    '- Action requests arrive as messages starting with "Thingy action" and a JSON object with action_id, action and params. For each one, set it running with sys_thingy_action_update, carry it out as its description in sys_thingy_describe says, then set it done or failed with a short Markdown summary. Take the current version from sys_thingy_action_list before each update.',
    "- Stay free to take the next request: hand long or independent work to a child session (sys_session_create) with the action_id and these reporting rules, and record its session ID on the action. Run actions that touch the same records one at a time. If a request repeats one that is still queued or running, mark it done and say it was merged.",
    "- The ledger, not this conversation, records what is done; after a restart or compaction, read it again.",
    "- Read and change records with the sys_thingy_record_* tools; a write that breaks one of the thingy's rules fails with CONSTRAINT_VIOLATION.",
    "- To change the thingy itself, read sys_thingy_revision_get, upload a new revision with sys_thingy_revision_create (collection schemas cannot change; add a collection if you need one) and publish it with sys_thingy_publish using the current version from sys_thingy_get. Keep a pending state on every action, since data calls often take a second or more. Do not edit or delete records unless I ask.",
  ].join("\n");
}

/** The message that hands one ledger entry to the thingy session. */
export function thingyActionMessage(entry: {
  id: string;
  action: string;
  params: Record<string, unknown>;
}): string {
  return `Thingy action\n${JSON.stringify({ action_id: entry.id, action: entry.action, params: entry.params })}`;
}

/** Remembers that this tab opened New session for the thingy's session, so it is adopted once started. */
export function markThingySessionPending(appId: string): void {
  markPending(appId);
}

function markPending(appId: string, actionId?: string): void {
  pendingStarts.set(appId, {
    at: Date.now(),
    actionIds: actionId ? [actionId] : [],
  });
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
  markPending(app.id, actionId);
  // The request waits in New session's composer, which a full screen thingy would hide.
  exitThingyRailFullScreen(null);
  navigate(thingyBoardNewSessionUrl(app.id, `${composeThingySessionPrompt(app)}\n\n${message}`));
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
  if (entry.merged) {
    // The same request is still open, so nothing is sent again.
    await openThingySession(app, "", navigate, {
      deliver: false,
      reveal: false,
      railSessionId,
    });
    return { actionId: entry.id, delivery: "merged" };
  }
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
  exitThingyRailFullScreen(null);
  if (app.permissions.publish) {
    markPending(app.id);
    navigate(thingyBoardNewSessionUrl(app.id, `${composeThingySessionPrompt(app)}\n\n${quoted}`));
    return;
  }
  navigate(thingyBoardNewSessionUrl(app.id, composeThingyContextPrompt(quoted, app)));
}
