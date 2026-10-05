// Reads for the Design page. Deck and kit reads go through the existing file
// APIs, so a user only sees decks in workspaces they can already read.

import { fetchFileContent } from "@/hooks/useFileContent";
import { fetchHostFilesystem } from "@/hooks/useHostFilesystem";
import { writeFileContent } from "@/hooks/useWriteFileContent";
import { authenticatedFetch } from "./identity";
import { isOwnerLevel } from "./permissionsApi";
import { getSessionSlim } from "./sessionsApi";
import {
  WORKSPACE_FILE_SEARCH_LIMIT,
  readWorkspaceFileSearch,
  requestWorkspaceFileSearch,
} from "@/hooks/useWorkspaceChangedFiles";
import { DESIGN_KIT_DIR, kitText } from "@/shell/codeViewerHelpers";
import {
  DESIGN_SYSTEM_IMPORT_DIR,
  DESIGN_SYSTEM_POINTER,
  isAbsoluteDesignSystemPath,
  parseDesignSystemPointer,
  type DesignSystemRef,
} from "./designSystem";
import {
  importDesignSystem,
  listDesignSystemSource,
  planDesignSystemImport,
  type ImportPlan,
} from "./designSystemImport";
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
  const imported = `/${DESIGN_SYSTEM_IMPORT_DIR}/`;
  const paths = files
    .filter((f) => f.type === "file" && !`/${f.path}`.includes(imported))
    .map((f) => f.path);
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

/**
 * The outside design system a session's owner can import, or `null` when the
 * pointer is missing, invalid, already imported, or the viewer is not the owner.
 */
export async function fetchImportTarget(
  sessionId: string,
): Promise<{ hostId: string; source: DesignSystemRef } | null> {
  const [session, pointer] = await Promise.all([
    getSessionSlim(sessionId),
    fetchFileContent(sessionId, DESIGN_SYSTEM_POINTER).catch(() => null),
  ]);
  if (!pointer || !session.hostId || !isOwnerLevel(session.permissionLevel)) return null;
  try {
    const source = parseDesignSystemPointer(kitText(pointer, "design-system.json"));
    return isAbsoluteDesignSystemPath(source.path) ? { hostId: session.hostId, source } : null;
  } catch {
    return null;
  }
}

/** The import plan for a design-system folder on a host. */
export async function planDesignSystemImportFrom(
  hostId: string,
  folder: string,
): Promise<ImportPlan> {
  const listing = await listDesignSystemSource(folder, (dir) => fetchHostFilesystem(hostId, dir));
  return planDesignSystemImport(listing);
}

/** Copy `plan` from `source` into the session's workspace, then point the design at the copy. */
export function runDesignSystemImport(
  sessionId: string,
  plan: ImportPlan,
  source: DesignSystemRef,
  onProgress: (done: number, total: number) => void,
) {
  return importDesignSystem(plan, source, {
    read: (rel) => fetchFileContent(sessionId, `${source.path.replace(/[/\\]+$/, "")}/${rel}`),
    write: (path, content, encoding) => writeFileContent(sessionId, path, content, encoding),
    onProgress,
  });
}

/** Replace a session's indexed decks with a successful scan's paths. Best-effort. */
export async function reconcileDesignIndex(sessionId: string, paths: string[]): Promise<void> {
  await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/design-artifacts`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths, kind: "deck" }),
  }).catch(() => undefined);
}
