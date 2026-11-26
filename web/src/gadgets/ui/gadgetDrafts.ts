// Sessions with a New gadget tab open in their side panel, whose gadget isn't created yet, and
// the description typed there. Kept for the browser tab (sessionStorage), so a reload or a visit
// to another session keeps the tab and its text.
import { create } from "zustand";

import type { Gadget } from "@/gadgets/api/gadgetApi";

const STORAGE_KEY = "omnigent:gadget-drafts";
// Keeps the store small if many sessions leave a draft open.
const MAX_DRAFTS = 20;

interface GadgetDraftsState {
  /** The description typed in each session's New gadget tab, by session ID, oldest first. */
  drafts: ReadonlyMap<string, string>;
}

function readDrafts(): Map<string, string> {
  if (typeof window === "undefined") return new Map();
  try {
    const parsed: unknown = JSON.parse(window.sessionStorage.getItem(STORAGE_KEY) ?? "[]");
    if (!Array.isArray(parsed)) return new Map();
    return new Map(
      parsed.filter(
        (entry): entry is [string, string] =>
          Array.isArray(entry) && typeof entry[0] === "string" && typeof entry[1] === "string",
      ),
    );
  } catch {
    return new Map();
  }
}

function writeDrafts(drafts: ReadonlyMap<string, string>): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify([...drafts]));
  } catch {
    // Storage refused (quota, privacy mode): the draft lasts until the page reloads.
  }
}

export const useGadgetDrafts = create<GadgetDraftsState>(() => ({ drafts: readDrafts() }));

function update(change: (drafts: Map<string, string>) => void): void {
  useGadgetDrafts.setState((state) => {
    const drafts = new Map(state.drafts);
    change(drafts);
    for (const id of [...drafts.keys()].slice(0, Math.max(0, drafts.size - MAX_DRAFTS)))
      drafts.delete(id);
    writeDrafts(drafts);
    return { drafts };
  });
}

export function useHasGadgetDraft(sessionId: string): boolean {
  return useGadgetDrafts((state) => state.drafts.has(sessionId));
}

/** Opens or closes a session's New gadget tab; closing it drops its text. */
export function setGadgetDraft(sessionId: string, open: boolean): void {
  if (open === useGadgetDrafts.getState().drafts.has(sessionId)) return;
  update((drafts) => {
    if (open) drafts.set(sessionId, "");
    else drafts.delete(sessionId);
  });
}

/** The description typed in a session's New gadget tab so far. */
export function gadgetDraftText(sessionId: string): string {
  return useGadgetDrafts.getState().drafts.get(sessionId) ?? "";
}

export function setGadgetDraftText(sessionId: string, text: string): void {
  const { drafts } = useGadgetDrafts.getState();
  if (!drafts.has(sessionId) || drafts.get(sessionId) === text) return;
  update((next) => {
    // Moves the session last, so the oldest drafts go first past the limit.
    next.delete(sessionId);
    next.set(sessionId, text);
  });
}

/** A build that failed after creating its gadget, and the description it was for. */
export interface UnfinishedGadgetBuild {
  description: string;
  gadget: Gadget;
  /**
   * Its build message may have reached the session (sent, but no answer came back), so the
   * session may be building it: it is kept rather than deleted when the draft is dropped.
   */
  maybeDelivered?: boolean;
}

// Per session, in memory: switching sessions and back keeps it, so Try again reuses the gadget
// and Cancel deletes it, instead of leaving it behind.
const unfinishedBuilds = new Map<string, UnfinishedGadgetBuild>();

export function unfinishedGadgetBuild(sessionId: string): UnfinishedGadgetBuild | null {
  return unfinishedBuilds.get(sessionId) ?? null;
}

export function setUnfinishedGadgetBuild(
  sessionId: string,
  build: UnfinishedGadgetBuild | null,
): void {
  if (build === null) unfinishedBuilds.delete(sessionId);
  else unfinishedBuilds.set(sessionId, build);
}

/**
 * The session's build in progress: which run is current (a newer run or Cancel bumps it, and
 * the older run's steps stop), and whether its build message is on its way. Per session, not
 * per pane, so a pane remounted after a visit to another session owns the same run.
 */
export interface GadgetBuildRun {
  run: number;
  /** The run whose build message is on its way, or `null`. */
  delivering: number | null;
}

const buildRuns = new Map<string, GadgetBuildRun>();

export function gadgetBuildRun(sessionId: string): GadgetBuildRun {
  let state = buildRuns.get(sessionId);
  if (state === undefined) {
    state = { run: 0, delivering: null };
    buildRuns.set(sessionId, state);
  }
  return state;
}

/** Forgets every draft; for tests. */
export function resetGadgetDrafts(): void {
  writeDrafts(new Map());
  unfinishedBuilds.clear();
  buildRuns.clear();
  useGadgetDrafts.setState({ drafts: new Map() });
}
