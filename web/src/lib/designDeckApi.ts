// Reads for the Design page. Deck and kit reads go through the existing file
// APIs, so a user only sees decks in workspaces they can already read.

import { fetchFileContent } from "@/hooks/useFileContent";
import { authenticatedFetch } from "./identity";
import {
  WORKSPACE_FILE_SEARCH_LIMIT,
  readWorkspaceFileSearch,
  requestWorkspaceFileSearch,
} from "@/hooks/useWorkspaceChangedFiles";
import { DESIGN_KIT_DIR, kitText } from "@/shell/codeViewerHelpers";
import { DESIGN_SYSTEM_POINTER, parseDesignSystemPointer } from "./designSystem";
import {
  DECK_INCLUDE_GLOB,
  DECK_SEARCH_QUERY,
  kitIndicator,
  type DesignIndexEntry,
  type KitIndicator,
} from "./designDecks";

export type DeckSearchResult =
  { status: "ok"; paths: string[]; truncated: boolean } | { status: "unavailable" };

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
  const { files, truncated, hasMore } = await readWorkspaceFileSearch(res);
  const paths = files.filter((f) => f.type === "file").map((f) => f.path);
  // Scan budget (`truncated`) or a full page of results (the server's `has_more`).
  // Count is only a stand-in when `has_more` is missing from the body.
  return {
    status: "ok",
    paths,
    truncated: truncated || hasMore || files.length >= WORKSPACE_FILE_SEARCH_LIMIT,
  };
}

/**
 * The group indicator for a workspace: its design-system pointer when there is
 * one, else its kit from `kit.json` alone (no assets).
 */
export async function fetchKitIndicator(sessionId: string): Promise<KitIndicator> {
  // A failed pointer read falls through to the kit read, which names the failure.
  const pointer = await fetchFileContent(sessionId, DESIGN_SYSTEM_POINTER).catch(() => null);
  if (pointer) {
    try {
      const ref = parseDesignSystemPointer(kitText(pointer, "design-system.json"));
      return { status: "system", name: ref.name, kind: ref.kind };
    } catch (e) {
      return {
        status: "invalid",
        reason: e instanceof Error ? e.message : String(e),
        system: true,
      };
    }
  }
  try {
    return kitIndicator(await fetchFileContent(sessionId, `${DESIGN_KIT_DIR}/kit.json`));
  } catch (e) {
    const reason = e instanceof Error ? e.message : String(e);
    return reason.startsWith("404") ? { status: "none" } : { status: "invalid", reason };
  }
}

/**
 * The server's deck index, or `null` when it is unavailable (an older server,
 * the `design` flag off, or a failure) so the page falls back to scanning.
 */
export async function fetchDesignIndex(): Promise<DesignIndexEntry[] | null> {
  const res = await authenticatedFetch("/v1/design/artifacts?kind=deck");
  if (!res.ok) return null;
  return ((await res.json()) as { data: DesignIndexEntry[] }).data;
}

/** Replace a session's indexed decks with a successful scan's paths. Best-effort. */
export async function reconcileDesignIndex(sessionId: string, paths: string[]): Promise<void> {
  await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/design-artifacts`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths, kind: "deck" }),
  }).catch(() => undefined);
}
