// New session opened from a gadget carries the gadget's ID in its URL (`gadget_start`). New
// session creates the session at its usual path; once that path has the real session ID, the
// gadget is linked to the session and shown in its side panel. A host whose navigation keeps the
// query brings `gadget_start` along to the session's page; it is adopted there and dropped from
// the URL, so a reload does not adopt it again.
//
// A gadget that Build (or the blank template) just created exists only to be built by that
// session. Leaving New session without starting it deletes the gadget again, while it has nothing
// published and no session, so abandoned attempts don't pile up in the viewer's list.
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
import { invalidateGadgets, useGadgetsEnabled } from "./gadgetQueries";

// The gadget whose New session this tab is on, until it starts a session or goes elsewhere.
let starting: string | null = null;
// Gadgets this tab created for New session to build, until a session adopts them.
const unbuilt = new Set<string>();
// Gadgets filling the screen beside New session: the session started there shows them full
// screen too, so creating it from the Create session menu keeps the view.
const fullScreenStarts = new Set<string>();

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
  if (starting === gadgetId) starting = null;
  fullScreenStarts.delete(gadgetId);
  void discardIfUnbuilt(gadgetId, queryClient);
}

/** Forgets every started gadget; for tests. */
export function resetGadgetStarts(): void {
  starting = null;
  unbuilt.clear();
  fullScreenStarts.clear();
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
    const params = new URLSearchParams(search);
    const marked = params.get(GADGET_START_PARAM);
    const sessionId = pathname.startsWith(sessionPrefix)
      ? decodeURIComponent(pathname.slice(sessionPrefix.length).replace(/\/$/, ""))
      : null;
    // The temporary ID while the session is created; the real one replaces it.
    const started = sessionId !== null && isStartedSessionId(sessionId);
    if (marked && !started) {
      if (starting !== null && starting !== marked) leaveGadgetStart(starting, queryClient);
      starting = marked;
      return;
    }
    const gadgetId = marked ?? starting;
    if (gadgetId === null) return;
    if (sessionId === null) {
      // Left New session without starting it: for another page, or a plain New session.
      leaveGadgetStart(gadgetId, queryClient);
      return;
    }
    if (!started) return;
    if (starting !== null && starting !== gadgetId) leaveGadgetStart(starting, queryClient);
    if (marked) {
      params.delete(GADGET_START_PARAM);
      replaceSearchParams(params);
    }
    starting = null;
    unbuilt.delete(gadgetId);
    const fullScreen = fullScreenStarts.delete(gadgetId);
    adoptStartedGadgetSession(gadgetId, sessionId)
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
  }, [enabled, pathname, queryClient, search, sessionPrefix, replaceSearchParams]);
}
