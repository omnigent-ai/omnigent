// Reads for the Design page. Deck and kit reads go through the existing file
// APIs, so a user only sees decks in workspaces they can already read.

import { useQuery } from "@tanstack/react-query";
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
import { DESIGN_KIT_DIR, bytesToBase64, kitText, parseDesignKit } from "@/shell/codeViewerHelpers";
import {
  DESIGN_SYSTEM_IMPORT_DIR,
  DESIGN_SYSTEM_POINTER,
  isAbsoluteDesignSystemPath,
  parseDesignSystemPointer,
  type DesignDefault,
  type DesignSystemRef,
} from "./designSystem";
import { DS_ASSET_MAX_BYTES, DS_DECK_MAX_BYTES } from "./designSystemInjection";
import {
  importDesignSystem,
  listDesignSystemSource,
  planDesignSystemImport,
  type ImportPlan,
} from "./designSystemImport";
import {
  DESIGN_INCLUDE_GLOB,
  DESIGN_SEARCH_QUERY,
  DESIGN_SUFFIXES,
  designKind,
  kitIndicator,
  type DesignIndexEntry,
  type DesignKind,
  type KitIndicator,
} from "./designDecks";

export type DeckSearchResult =
  { status: "ok"; paths: string[]; truncated: boolean } | { status: "unavailable" };

/**
 * Every `*.slides.html` and `*.wireframe.html` path in a session's workspace.
 * A 404 (no file environment) or 503 (runner offline) is "unavailable" rather
 * than empty; any other failure throws.
 */
export async function fetchDesignSearch(sessionId: string): Promise<DeckSearchResult> {
  const res = await requestWorkspaceFileSearch(sessionId, {
    query: DESIGN_SEARCH_QUERY,
    include: DESIGN_INCLUDE_GLOB,
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
 * The server's index of every kind, or `null` when it is unavailable (an older
 * server, the `design` flag off, or a failure) so the page falls back to scanning.
 */
export async function fetchDesignIndex(): Promise<DesignIndexEntry[] | null> {
  const res = await authenticatedFetch("/v1/design/artifacts");
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

/** Replace a session's indexed rows of each kind with a successful scan's paths. Best-effort. */
export async function reconcileDesignIndex(sessionId: string, paths: string[]): Promise<void> {
  for (const kind of Object.keys(DESIGN_SUFFIXES) as DesignKind[]) {
    // oxlint-disable-next-line no-await-in-loop
    await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/design-artifacts`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: paths.filter((p) => designKind(p) === kind), kind }),
    }).catch(() => undefined);
  }
}

const DESIGN_DEFAULT_URL = "/v1/me/preferences/design-default";

/** The user's New design default, or `null` when unset or unavailable (flag off, older server). */
export async function fetchDesignDefault(): Promise<DesignDefault | null> {
  const res = await authenticatedFetch(DESIGN_DEFAULT_URL);
  if (!res.ok) return null;
  const { design_default: value } = (await res.json()) as {
    design_default: { kind: string; host_id?: string; path?: string; name?: string } | null;
  };
  if (!value) return null;
  if (value.kind === "none") return { kind: "none" };
  try {
    const ref = parseDesignSystemPointer(JSON.stringify(value));
    return value.host_id ? { ...ref, hostId: value.host_id } : null;
  } catch {
    return null;
  }
}

export const DESIGN_DEFAULT_QUERY_KEY = ["design-default"];

export function useDesignDefault(enabled: boolean) {
  return useQuery({
    queryKey: DESIGN_DEFAULT_QUERY_KEY,
    queryFn: fetchDesignDefault,
    enabled,
    retry: false,
  });
}

export async function saveDesignDefault(value: DesignDefault): Promise<void> {
  const body =
    value.kind === "none"
      ? value
      : { kind: value.kind, host_id: value.hostId, path: value.path, name: value.name };
  const res = await authenticatedFetch(DESIGN_DEFAULT_URL, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ design_default: body }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
}

const MB = 1024 * 1024;

async function readOrgKitFile(rel: string): Promise<Uint8Array> {
  const res = await authenticatedFetch(
    `/v1/design-kit/${rel.split("/").map(encodeURIComponent).join("/")}`,
  );
  if (!res.ok) throw new Error(`${rel}: ${res.status} ${res.statusText}`);
  const bytes = new Uint8Array(await res.arrayBuffer());
  if (bytes.length > DS_ASSET_MAX_BYTES) {
    throw new Error(`${rel} is larger than ${DS_ASSET_MAX_BYTES / MB} MB`);
  }
  return bytes;
}

/**
 * Copy the organization kit into the session's `.omnigent/design-kit/`: the
 * files its kit.json uses, read in full first, then written with kit.json last.
 */
export async function materializeOrgKit(sessionId: string): Promise<void> {
  const kitJson = await readOrgKitFile("kit.json");
  const kit = parseDesignKit(new TextDecoder().decode(kitJson));
  const used = [kit.css, kit.logo?.src, kit.fonts.heading?.src, kit.fonts.body?.src];
  const rels = [...new Set(used.filter((rel): rel is string => !!rel))];
  const assets = await Promise.all(rels.map(readOrgKitFile));
  const total = assets.reduce((sum, bytes) => sum + bytes.length, kitJson.length);
  if (total > DS_DECK_MAX_BYTES) throw new Error(`over the ${DS_DECK_MAX_BYTES / MB} MB total`);
  const write = (rel: string, bytes: Uint8Array) =>
    writeFileContent(sessionId, `${DESIGN_KIT_DIR}/${rel}`, bytesToBase64(bytes), "base64");
  await Promise.all(rels.map((rel, i) => write(rel, assets[i])));
  await write("kit.json", kitJson);
}
