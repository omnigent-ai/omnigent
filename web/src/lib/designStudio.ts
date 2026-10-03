// Pure helpers for the Design studio: deck slugs, the first message, the
// studio URL, remembered dialog defaults, and the preview's waiting states.

import { DECK_SUFFIX } from "./designDecks";

export const DESIGN_SESSION_PARAM = "session";
export const DESIGN_FILE_PARAM = "file";
export const DESIGN_VIEW_PARAM = "view";

/** `chat` is the phone's full-screen chat; desktop treats it as `preview`. */
export type StudioView = "preview" | "full" | "chat";

export interface StudioParams {
  sessionId: string;
  path: string;
  view: StudioView;
}

export function readStudioParams(params: URLSearchParams): StudioParams | null {
  const sessionId = params.get(DESIGN_SESSION_PARAM);
  const path = params.get(DESIGN_FILE_PARAM);
  if (!sessionId || !path) return null;
  const raw = params.get(DESIGN_VIEW_PARAM);
  const view: StudioView = raw === "full" || raw === "chat" ? raw : "preview";
  return { sessionId, path, view };
}

export function studioHref(sessionId: string, path: string, view: StudioView = "preview"): string {
  const params = new URLSearchParams({
    [DESIGN_SESSION_PARAM]: sessionId,
    [DESIGN_FILE_PARAM]: path,
  });
  if (view !== "preview") params.set(DESIGN_VIEW_PARAM, view);
  return `/design?${params}`;
}

export const DECK_SLUG_MAX = 40;

/** The prompt's first words in kebab case, with `-2`, `-3`... when `taken` has it. */
export function deckSlug(prompt: string, taken: Iterable<string>): string {
  const words = prompt
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
  let base = "";
  for (const word of words) {
    const next = base ? `${base}-${word}` : word;
    if (next.length > DECK_SLUG_MAX) break;
    base = next;
  }
  if (!base) base = words[0]?.slice(0, DECK_SLUG_MAX) ?? "deck";
  const used = new Set(taken);
  let slug = base;
  for (let n = 2; used.has(slug); n++) slug = `${base}-${n}`;
  return slug;
}

export function designDeckPath(slug: string): string {
  return `decks/${slug}${DECK_SUFFIX}`;
}

export function firstDesignMessage(prompt: string, path: string): string {
  return (
    `${prompt.trim()}\n\nUse the slide-decks skill. Write the deck to \`${path}\`. ` +
    "Write a complete document with only the title slide first, then add one complete " +
    "slide per edit."
  );
}

export interface DesignDefaults {
  agentId?: string;
  hostId?: string;
  /** Last folder used for a design, per host id. */
  folders?: Record<string, string>;
}

const DEFAULTS_KEY = "omnigent.design.defaults";

export function readDesignDefaults(): DesignDefaults {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(DEFAULTS_KEY) ?? "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as DesignDefaults)
      : {};
  } catch {
    return {};
  }
}

export function rememberDesignDefaults(agentId: string, hostId: string, folder: string): void {
  const prev = readDesignDefaults();
  const next: DesignDefaults = {
    agentId,
    hostId,
    folders: { ...prev.folders, [hostId]: folder },
  };
  try {
    localStorage.setItem(DEFAULTS_KEY, JSON.stringify(next));
  } catch {
    // Storage disabled or full: the dialog just won't prefill next time.
  }
}

export type DeckPreviewState = "loading" | "deck" | "waiting" | "not-written" | "error";

/** A missing deck is "waiting" until the agent's turn ends without writing it. */
export function deckPreviewState(input: {
  file: "loading" | "ok" | "missing" | "error";
  turnEnded: boolean;
}): DeckPreviewState {
  if (input.file === "ok") return "deck";
  if (input.file === "missing") return input.turnEnded ? "not-written" : "waiting";
  return input.file;
}

export const DESIGN_SUGGESTIONS: readonly { id: string; title: string }[] = [
  { id: "pitch", title: "Pitch deck from my notes" },
  { id: "status", title: "Weekly status update" },
  { id: "launch", title: "Product launch" },
];
