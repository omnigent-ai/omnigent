// Session-scoped reads for the Design page. Both go through the existing file
// APIs, so a user only sees decks in workspaces they can already read.

import { fetchFileContent } from "@/hooks/useFileContent";
import {
  readWorkspaceFileSearch,
  requestWorkspaceFileSearch,
} from "@/hooks/useWorkspaceChangedFiles";
import { DESIGN_KIT_DIR } from "@/shell/codeViewerHelpers";
import {
  DECK_INCLUDE_GLOB,
  DECK_SEARCH_QUERY,
  kitIndicator,
  type KitIndicator,
} from "./designDecks";

export type DeckSearchResult = { status: "ok"; paths: string[] } | { status: "unavailable" };

/**
 * Every `*.slides.html` path in a session's workspace. A 404 (no file
 * environment) or 503 (runner offline) is "unavailable" rather than empty;
 * any other failure throws.
 */
export async function fetchDeckSearch(sessionId: string): Promise<DeckSearchResult> {
  const res = await requestWorkspaceFileSearch(sessionId, {
    query: DECK_SEARCH_QUERY,
    include: DECK_INCLUDE_GLOB,
  });
  if (res.status === 404 || res.status === 503) return { status: "unavailable" };
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const { files } = await readWorkspaceFileSearch(res);
  return { status: "ok", paths: files.filter((f) => f.type === "file").map((f) => f.path) };
}

/** The kit indicator for a workspace, reading `kit.json` only (no assets). */
export async function fetchKitIndicator(sessionId: string): Promise<KitIndicator> {
  try {
    return kitIndicator(await fetchFileContent(sessionId, `${DESIGN_KIT_DIR}/kit.json`));
  } catch (e) {
    const reason = e instanceof Error ? e.message : String(e);
    return reason.startsWith("404") ? { status: "none" } : { status: "invalid", reason };
  }
}
