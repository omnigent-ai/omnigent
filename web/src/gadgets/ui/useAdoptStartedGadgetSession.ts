// Links a gadget to its newly created session and discards an unbuilt gadget left unsent.
import { useEffect } from "react";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";

import { useLocation, useRebasePath, useReplaceSearchParams } from "@/lib/routing";
import { noteGadgetStartFailed, requestGadgetRailTab } from "@/lib/gadgetRailTabs";
import { discardUnbuiltGadget, getGadget, getGadgetGadgetSession } from "@/gadgets/api/gadgetApi";
import { GADGET_START_PARAM, sessionIdFromPath } from "@/gadgets/host/gadgetPages";
import {
  acknowledgeStartedGadgetInput,
  adoptStartedGadgetSession,
  forgetUnrecordedDeliveries,
  isStartedSessionId,
  gadgetRailKey,
  keepUnrecordedDeliveries,
  recordUnrecordedDeliveries,
  setGadgetSessionStarting,
} from "@/gadgets/host/gadgetSession";
import {
  acceptedFirstInput,
  onFirstInputAccepted,
  onSessionCreatedHere,
  onSessionCreationAttempt,
} from "@/lib/sessionCreations";
import { invalidateGadgets, useGadgetsEnabled } from "./gadgetQueries";

// The gadget whose New session this tab is on, until it starts a session or goes elsewhere.
let starting: { gadgetId: string } | null = null;
// Creates that began on a gadget's New session, by attempt number: the session each makes is
// that gadget's, whatever page the viewer is on by then.
// `wasUnbuilt` says whether the attempt took the gadget's unbuilt mark, so a failed attempt
// gives back only a mark it took and never marks an existing gadget for deletion.
const attempts = new Map<number, { gadgetId: string; fullScreen: boolean; wasUnbuilt: boolean }>();
// Sessions an attempt made for a gadget, by session ID, until linking them succeeds; a page
// that shows the session again retries a link that failed.
const madeFor = new Map<string, { gadgetId: string; fullScreen: boolean }>();
// Sessions an attempt made for a gadget whose requests aren't counted delivered yet: that
// waits for both the link to the gadget (the server takes deliveries only for its gadget
// session) and the server accepting a first message, whose text says which requests it had.
const awaitingInput = new Map<
  string,
  { gadgetId: string; linked: boolean; accepted: string | null | undefined }
>();
// Gadget and session pairs linked or being linked, so a page that sees the session again does
// nothing.
const adopted = new Set<string>();
// Gadgets this tab created for New session to build, until a session adopts them.
const unbuilt = new Set<string>();
// Gadgets filling the screen beside New session: the session started there shows them full
// screen too, so creating it from the Create session menu keeps the view.
const fullScreenStarts = new Set<string>();
let listening = false;
// Discards waiting on the server, by gadget: going back to the gadget's New session, a create
// beginning for it or a session adopting it takes the gadget out, so the discard stops.
const discarding = new Map<string, number>();
let discardCount = 0;

function stopDiscard(gadgetId: string): boolean {
  return discarding.delete(gadgetId);
}

/** Notes whether the gadget beside New session fills the screen, for the session started there. */
export function noteGadgetStartFullScreen(gadgetId: string, fullScreen: boolean): void {
  if (fullScreen) fullScreenStarts.add(gadgetId);
  else fullScreenStarts.delete(gadgetId);
}

/** Marks a gadget as created for New session to build; it goes again if New session is left. */
export function rememberUnbuiltGadget(gadgetId: string): void {
  unbuilt.add(gadgetId);
}

function creating(gadgetId: string): boolean {
  for (const attempt of attempts.values()) if (attempt.gadgetId === gadgetId) return true;
  return false;
}

async function discardIfUnbuilt(gadgetId: string, queryClient: QueryClient): Promise<void> {
  // A sent Build whose session is still being created keeps its gadget.
  if (creating(gadgetId) || !unbuilt.delete(gadgetId)) return;
  const token = ++discardCount;
  discarding.set(gadgetId, token);
  // Still to be deleted: the viewer hasn't returned to its New session and no create or
  // session has taken it since this discard began.
  const stillDiscarding = () =>
    discarding.get(gadgetId) === token && !creating(gadgetId) && starting?.gadgetId !== gadgetId;
  try {
    const gadget = await getGadget(gadgetId);
    if (!stillDiscarding()) return;
    if (gadget.published_revision !== null || !gadget.permissions.manage) return;
    if ((await getGadgetGadgetSession(gadgetId)) !== null) return;
    if (!stillDiscarding()) return;
    // Checked again on the server with the delete: another tab may publish or adopt it.
    if (await discardUnbuiltGadget(gadgetId)) invalidateGadgets(queryClient);
  } catch {
    // It stays in the list, unpublished; the viewer can delete it there.
  } finally {
    if (discarding.get(gadgetId) === token) discarding.delete(gadgetId);
  }
}

/**
 * Stops treating New session as started from `gadgetId`, as when its panel is closed or the
 * gadget is deleted, so the next session started there stays plain.
 */
export function leaveGadgetStart(gadgetId: string, queryClient: QueryClient): void {
  if (starting?.gadgetId === gadgetId) starting = null;
  fullScreenStarts.delete(gadgetId);
  void discardIfUnbuilt(gadgetId, queryClient);
}

/** Forgets every started gadget; for tests. */
export function resetGadgetStarts(): void {
  for (const attempt of attempts.values()) setGadgetSessionStarting(attempt.gadgetId, false);
  for (const waiting of awaitingInput.values()) setGadgetSessionStarting(waiting.gadgetId, false);
  starting = null;
  attempts.clear();
  madeFor.clear();
  adopted.clear();
  unbuilt.clear();
  fullScreenStarts.clear();
  discarding.clear();
  awaitingInput.clear();
  forgetUnrecordedDeliveries();
}

function beginGadgetStart(gadgetId: string, queryClient: QueryClient): void {
  if (starting?.gadgetId === gadgetId) return;
  if (starting !== null) leaveGadgetStart(starting.gadgetId, queryClient);
  // Back on New session before the discard finished: the gadget is unbuilt again.
  if (stopDiscard(gadgetId)) unbuilt.add(gadgetId);
  starting = { gadgetId };
}

// A create began, or failed. One that begins while a gadget's New session is open is that
// gadget's; a failed one links nothing, and the gadget waits for the next attempt.
function onAttempt(attempt: number, outcome: "begun" | "failed"): void {
  if (outcome === "failed") {
    const failed = attempts.get(attempt);
    attempts.delete(attempt);
    // Build's gadget stays unbuilt: leaving New session deletes it as if Build was never sent.
    if (failed?.wasUnbuilt) unbuilt.add(failed.gadgetId);
    if (failed !== undefined && !creating(failed.gadgetId)) {
      setGadgetSessionStarting(failed.gadgetId, false);
    }
    return;
  }
  if (starting === null) return;
  const gadgetId = starting.gadgetId;
  const wasUnbuilt = unbuilt.delete(gadgetId) || stopDiscard(gadgetId);
  attempts.set(attempt, { gadgetId, fullScreen: fullScreenStarts.has(gadgetId), wasUnbuilt });
  // The gadget waits from Create session until its session has its requests.
  setGadgetSessionStarting(gadgetId, true);
}

function adopt(gadgetId: string, sessionId: string, fullScreen: boolean) {
  const pair = `${gadgetId}\n${sessionId}`;
  if (adopted.has(pair)) return;
  adopted.add(pair);
  if (starting?.gadgetId === gadgetId) starting = null;
  unbuilt.delete(gadgetId);
  stopDiscard(gadgetId);
  fullScreenStarts.delete(gadgetId);
  madeFor.set(sessionId, { gadgetId, fullScreen });
  // A link tried again keeps what the first try learned of the session's first message.
  if (!awaitingInput.has(sessionId)) {
    awaitingInput.set(sessionId, {
      gadgetId,
      linked: false,
      accepted: acceptedFirstInput(sessionId),
    });
  }
  adoptStartedGadgetSession(gadgetId, sessionId)
    .then(() => {
      madeFor.delete(sessionId);
      const waiting = awaitingInput.get(sessionId);
      if (waiting !== undefined) waiting.linked = true;
      acknowledgeIfReady(sessionId);
      const key = gadgetRailKey(gadgetId);
      // Started beside a full screen gadget: it stays full screen, the sidebar as it was.
      if (fullScreen) requestGadgetRailTab(sessionId, key, { maximize: true, keepSidebar: true });
      else requestGadgetRailTab(sessionId, key);
    })
    // The session still runs; the gadget stays on its own page until a later visit to the
    // session links it again.
    .catch(() => {
      adopted.delete(pair);
      awaitingInput.delete(sessionId);
      setGadgetSessionStarting(gadgetId, false);
      if (fullScreen) noteGadgetStartFailed(gadgetId);
    });
}

// The first message of a session made for a gadget was accepted, with `text`.
function onAccepted(sessionId: string, text: string | null): void {
  const waiting = awaitingInput.get(sessionId);
  if (waiting === undefined) return;
  waiting.accepted = text;
  acknowledgeIfReady(sessionId);
}

// Linked and its first message accepted: the requests that message carried were delivered.
function acknowledgeIfReady(sessionId: string): void {
  const waiting = awaitingInput.get(sessionId);
  if (waiting === undefined || !waiting.linked || waiting.accepted === undefined) return;
  awaitingInput.delete(sessionId);
  void acknowledgeStartedGadgetInput(waiting.gadgetId, sessionId, waiting.accepted)
    .then(
      // Deliveries whose records failed are kept, and tried again when the session shows.
      (failed) => keepUnrecordedDeliveries(waiting.gadgetId, sessionId, failed),
      () => undefined,
    )
    // Its requests are counted: the gadget's own actions show where they are from here.
    .finally(() => setGadgetSessionStarting(waiting.gadgetId, false));
}

// A session this tab created: the gadget's when the create that made it was the gadget's.
function onCreated(
  sessionId: string,
  _firstMessage: string | null,
  _seq: number,
  attempt: number | null,
): void {
  if (attempt === null || !isStartedSessionId(sessionId)) return;
  const claim = attempts.get(attempt);
  if (claim === undefined) return;
  attempts.delete(attempt);
  adopt(claim.gadgetId, sessionId, claim.fullScreen);
}

export function useAdoptStartedGadgetSession(): void {
  const { pathname, search } = useLocation();
  const rebasePath = useRebasePath();
  const queryClient = useQueryClient();
  const replaceSearchParams = useReplaceSearchParams();
  const sessionPrefix = rebasePath("/c/");
  const enabled = useGadgetsEnabled();
  useEffect(() => {
    // With the feature off, a `gadget_start` URL is left alone and nothing asks for gadgets.
    if (!enabled) return;
    if (!listening) {
      listening = true;
      onSessionCreatedHere(onCreated);
      onSessionCreationAttempt(onAttempt);
      onFirstInputAccepted(onAccepted);
    }
    const params = new URLSearchParams(search);
    const marked = params.get(GADGET_START_PARAM);
    const sessionId = sessionIdFromPath(pathname, sessionPrefix);
    if (sessionId === null) {
      if (marked) beginGadgetStart(marked, queryClient);
      // Left New session without starting it: for another page, or a plain New session.
      else if (starting !== null) leaveGadgetStart(starting.gadgetId, queryClient);
      return;
    }
    // The temporary ID while the session is created: Build was sent, and its attempt waits.
    if (!isStartedSessionId(sessionId)) return;
    if (marked) {
      params.delete(GADGET_START_PARAM);
      replaceSearchParams(params);
    }
    // Deliveries whose records failed earlier: try them again now the session shows.
    void recordUnrecordedDeliveries(sessionId);
    const made = madeFor.get(sessionId);
    if (made !== undefined) {
      // A link that failed earlier: try it again now the session shows.
      adopt(made.gadgetId, sessionId, made.fullScreen);
      return;
    }
    const gadgetId = marked ?? starting?.gadgetId ?? null;
    if (gadgetId === null || adopted.has(`${gadgetId}\n${sessionId}`)) return;
    // An existing session the viewer opened instead never got the gadget's requests. A sent
    // Build still waits for its own session.
    if (!creating(gadgetId)) leaveGadgetStart(gadgetId, queryClient);
    else if (starting?.gadgetId === gadgetId) starting = null;
  }, [enabled, pathname, queryClient, search, sessionPrefix, replaceSearchParams]);
}
