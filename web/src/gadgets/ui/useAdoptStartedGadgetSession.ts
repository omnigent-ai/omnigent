// New session opened from a gadget carries the gadget's ID in its URL (`gadget_start`). The
// session this tab creates from there, after the viewer opened it, is linked to the gadget and
// shows it in its side panel. That happens when the creation is recorded, whatever page the
// viewer is on by then: sending Build and then going elsewhere still links the session. Opening
// an existing session instead leaves the gadget unlinked, as leaving New session does, and so
// does a session this tab created before New session opened. A host whose navigation keeps the
// query brings `gadget_start` along to the session's page; it is dropped from the URL there, so
// a reload does not adopt it again.
//
// A gadget that Build (or the blank template) just created exists only to be built by that
// session. Leaving New session without starting it deletes the gadget again, while it has nothing
// published and no session, so abandoned attempts don't pile up in the viewer's list. Once the
// session is being created (its temporary ID shows), the gadget is kept for it.
import { useEffect } from "react";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";

import { useLocation, useRebasePath, useReplaceSearchParams } from "@/lib/routing";
import { noteGadgetStartFailed, requestGadgetRailTab } from "@/lib/gadgetRailTabs";
import { deleteGadget, getGadget, getGadgetGadgetSession } from "@/gadgets/api/gadgetApi";
import { GADGET_START_PARAM } from "@/gadgets/host/gadgetPages";
import {
  adoptStartedGadgetSession,
  isStartedSessionId,
  gadgetRailKey,
} from "@/gadgets/host/gadgetSession";
import {
  latestSessionCreation,
  onSessionCreatedHere,
  sessionCreatedHere,
} from "@/lib/sessionCreations";
import { invalidateGadgets, useGadgetsEnabled } from "./gadgetQueries";

// The gadget whose New session this tab is on, until it starts a session or goes elsewhere, and
// the latest session creation before it opened: only a later one is the gadget's.
let starting: { gadgetId: string; after: number } | null = null;
// Gadgets whose session is being created, by gadget ID: the next creation links them, whatever
// page the viewer is on by then.
const submitted = new Map<string, { after: number; fullScreen: boolean; at: number }>();
// How long a sent Build waits for its session to be created.
const SUBMITTED_TTL_MS = 10 * 60_000;
// Gadget and session pairs already linked, so a page that sees the session again does nothing.
const adopted = new Set<string>();
// Gadgets this tab created for New session to build, until a session adopts them.
const unbuilt = new Set<string>();
// Gadgets filling the screen beside New session: the session started there shows them full
// screen too, so creating it from the Create session menu keeps the view.
const fullScreenStarts = new Set<string>();
let listening = false;

/** Notes whether the gadget beside New session fills the screen, for the session started there. */
export function noteGadgetStartFullScreen(gadgetId: string, fullScreen: boolean): void {
  if (fullScreen) fullScreenStarts.add(gadgetId);
  else fullScreenStarts.delete(gadgetId);
}

/** Marks a gadget as created for New session to build; it goes again if New session is left. */
export function rememberUnbuiltGadget(gadgetId: string): void {
  unbuilt.add(gadgetId);
}

async function discardIfUnbuilt(gadgetId: string, queryClient: QueryClient): Promise<void> {
  if (!unbuilt.delete(gadgetId)) return;
  try {
    const gadget = await getGadget(gadgetId);
    if (gadget.published_revision !== null || !gadget.permissions.manage) return;
    if ((await getGadgetGadgetSession(gadgetId)) !== null) return;
    await deleteGadget(gadgetId);
    invalidateGadgets(queryClient);
  } catch {
    // It stays in the list, unpublished; the viewer can delete it there.
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
  starting = null;
  submitted.clear();
  adopted.clear();
  unbuilt.clear();
  fullScreenStarts.clear();
}

function beginGadgetStart(gadgetId: string, queryClient: QueryClient): void {
  if (starting?.gadgetId === gadgetId) return;
  if (starting !== null) leaveGadgetStart(starting.gadgetId, queryClient);
  starting = { gadgetId, after: latestSessionCreation() };
}

// Build was sent: keep the gadget for the session being created, even if the viewer leaves.
function claimGadgetStart(gadgetId: string): void {
  if (submitted.has(gadgetId)) return;
  for (const pair of adopted) if (pair.startsWith(`${gadgetId}\n`)) return;
  const after = starting?.gadgetId === gadgetId ? starting.after : latestSessionCreation();
  submitted.set(gadgetId, { after, fullScreen: fullScreenStarts.has(gadgetId), at: Date.now() });
  unbuilt.delete(gadgetId);
}

function adopt(
  gadgetId: string,
  sessionId: string,
  firstMessage: string | null,
  fullScreen: boolean,
) {
  const pair = `${gadgetId}\n${sessionId}`;
  if (adopted.has(pair)) return;
  adopted.add(pair);
  if (starting?.gadgetId === gadgetId) starting = null;
  submitted.delete(gadgetId);
  unbuilt.delete(gadgetId);
  fullScreenStarts.delete(gadgetId);
  adoptStartedGadgetSession(gadgetId, sessionId, firstMessage)
    .then(() => {
      const key = gadgetRailKey(gadgetId);
      // Started beside a full screen gadget: it stays full screen, the sidebar as it was.
      if (fullScreen) requestGadgetRailTab(sessionId, key, { maximize: true, keepSidebar: true });
      else requestGadgetRailTab(sessionId, key);
    })
    // The session still runs; the gadget stays on its own page.
    .catch(() => {
      if (fullScreen) noteGadgetStartFailed(gadgetId);
    });
}

// The gadget a session this tab just created belongs to: the oldest sent Build waiting for one,
// else the gadget whose New session is open, when the session was created after it opened.
function onCreated(sessionId: string, firstMessage: string | null, seq: number): void {
  if (!isStartedSessionId(sessionId)) return;
  const now = Date.now();
  for (const [gadgetId, claim] of submitted) {
    if (now - claim.at > SUBMITTED_TTL_MS) {
      submitted.delete(gadgetId);
      continue;
    }
    if (seq > claim.after) {
      adopt(gadgetId, sessionId, firstMessage, claim.fullScreen);
      return;
    }
  }
  if (starting !== null && seq > starting.after) {
    adopt(starting.gadgetId, sessionId, firstMessage, fullScreenStarts.has(starting.gadgetId));
  }
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
    }
    const params = new URLSearchParams(search);
    const marked = params.get(GADGET_START_PARAM);
    const sessionId = pathname.startsWith(sessionPrefix)
      ? decodeURIComponent(pathname.slice(sessionPrefix.length).replace(/\/$/, ""))
      : null;
    if (sessionId === null) {
      if (marked) beginGadgetStart(marked, queryClient);
      // Left New session without starting it: for another page, or a plain New session.
      else if (starting !== null) leaveGadgetStart(starting.gadgetId, queryClient);
      return;
    }
    const gadgetId = marked ?? starting?.gadgetId ?? null;
    if (!isStartedSessionId(sessionId)) {
      // The temporary ID while the session is created: Build was sent.
      if (gadgetId !== null) claimGadgetStart(gadgetId);
      return;
    }
    if (marked) {
      params.delete(GADGET_START_PARAM);
      replaceSearchParams(params);
    }
    if (gadgetId === null || adopted.has(`${gadgetId}\n${sessionId}`)) return;
    const creation = sessionCreatedHere(sessionId);
    const claim = submitted.get(gadgetId);
    const after = claim?.after ?? (starting?.gadgetId === gadgetId ? starting.after : null);
    if (creation !== undefined && after !== null && creation.seq > after) {
      adopt(
        gadgetId,
        sessionId,
        creation.firstMessage,
        claim?.fullScreen ?? fullScreenStarts.has(gadgetId),
      );
      return;
    }
    // An existing session the viewer opened instead never got the gadget's requests. A sent
    // Build still waits for its own session.
    if (claim === undefined) leaveGadgetStart(gadgetId, queryClient);
    else if (starting?.gadgetId === gadgetId) starting = null;
  }, [enabled, pathname, queryClient, search, sessionPrefix, replaceSearchParams]);
}
