// Sessions with a New gadget tab open in their side panel, whose gadget isn't created yet, and
// the description typed there. Kept for the browser tab (sessionStorage), so a reload or a visit
// to another session keeps the tab and its text.
import { create } from "zustand";

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

/** Forgets every draft; for tests. */
export function resetGadgetDrafts(): void {
  writeDrafts(new Map());
  useGadgetDrafts.setState({ drafts: new Map() });
}
