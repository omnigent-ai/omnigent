// Resolves a parsed file reference (see `lib/fileReferences`) against the
// session's filesystem, so every chat surface links a path the same way and
// only when it names something that exists.
//
// Each candidate is tried in order, the first that exists wins:
//   1. the agent-changed files list (synchronous, and the only source for a
//      deleted or not-yet-written file);
//   2. the candidate itself, relative to the working folder, `~`-expanded, or
//      absolute, checked against a cached listing of its parent directory;
//   3. a workspace search for a file whose path ends in the candidate, for a
//      path written relative to the repository root or a subfolder, used only
//      when exactly one file matches.

import { useEffect, useState } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import type { FileReference } from "@/lib/fileReferences";
import { worthSearching } from "@/lib/fileReferences";
import { absoluteWorkspacePath } from "@/lib/composerContext";
import {
  fetchWorkspaceFileSearch,
  resolveChatFilePath,
  useWorkspaceServeable,
  workspaceDirListingQuery,
  workspaceListingVerdict,
  workspaceParentDirectory,
  type DirListingPage,
} from "@/hooks/useWorkspaceChangedFiles";
import {
  type FilePosition,
  useFileViewer,
  useFileViewerConversationId,
  useFolderOpener,
  useIsChangedPath,
  useWorkspacePaths,
} from "@/shell/FileViewerContext";

/**
 * - `file` / `folder`: found; `open` shows it (null for a folder with no Files tree).
 * - `pending`: not verified yet (check in flight, errored, or root unknown).
 * - `missing`: every candidate was checked and none exists.
 * - `invalid`: no candidate resolves to a path the session could open.
 * - `unavailable`: this surface has no FileViewer, so nothing can open.
 */
export type FileReferenceStatus =
  "file" | "folder" | "pending" | "missing" | "invalid" | "unavailable";

export interface ResolvedFileReference {
  status: FileReferenceStatus;
  open: (() => void) | null;
  /** The path that opens (workspace-relative or host-absolute), or the first tried; "" when none. */
  path: string;
  /** Clipboard form: host-absolute when the workspace root is known, else as written. */
  copyPath: string;
  /** Index of the winning candidate in `reference.candidates`, or -1. */
  candidateIndex: number;
}

export interface FileReferenceOptions {
  /** Allow the workspace-wide search fallback for this reference. */
  search?: boolean;
  /** A position cited outside the reference text, e.g. `(lines 10–12)` after it. */
  position?: FilePosition | null;
}

type CandidateResolution = { path: string } | "pending" | null;

/**
 * Maps one candidate onto what the filesystem API opens, "pending" while the
 * workspace root (or home) it needs is unknown, or null when it never can.
 */
export function resolveReferenceCandidate(
  candidate: string,
  root: string | null,
  home: string | null,
): CandidateResolution {
  let text = candidate;
  if (text.startsWith("../")) {
    if (!root) return "pending";
    text = normalizeAbsolute(`${root.replace(/\/+$/, "")}/${text}`);
    if (!text) return null;
  }
  const resolution = resolveChatFilePath(text, root, home);
  if (resolution) return { path: resolution.path };
  if ((text.startsWith("/") && !root) || (text.startsWith("~") && !home)) return "pending";
  return null;
}

/** Collapses `.`/`..` segments of an absolute path; "" when it climbs above `/`. */
function normalizeAbsolute(path: string): string {
  const out: string[] = [];
  for (const segment of path.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") {
      if (!out.length) return "";
      out.pop();
    } else out.push(segment);
  }
  return `/${out.join("/")}`;
}

type CandidateState = "file" | "folder" | "missing" | "pending";

function candidateState(listing: DirListingPage | null | undefined, path: string): CandidateState {
  const verdict = workspaceListingVerdict(listing, path);
  if (verdict.exists) return "file";
  if (verdict.isDirectory) return "folder";
  return verdict.settled ? "missing" : "pending";
}

/** Resolves `reference` (null when the text isn't one) to an openable file or folder. */
export function useFileReference(
  reference: FileReference | null,
  options: FileReferenceOptions = {},
): ResolvedFileReference {
  const openFile = useFileViewer();
  const openFolder = useFolderOpener();
  const isChangedPath = useIsChangedPath();
  const conversationId = useFileViewerConversationId();
  const { root, home } = useWorkspacePaths();
  const serveable = useWorkspaceServeable(conversationId);

  // A miss counts only from a listing fetched after this reference first
  // rendered: an earlier mention (the user asking for `report.md`) may have
  // cached the directory before the agent wrote the file.
  const [renderedAt] = useState(() => Date.now());
  const candidates = reference?.candidates ?? [];
  const resolutions = candidates.map((candidate) =>
    resolveReferenceCandidate(candidate, root, home),
  );
  const resolvedPaths = resolutions.map((resolution) =>
    resolution && resolution !== "pending" ? resolution.path : null,
  );
  // A folder can't be in the changed list, and an outside path is never a change.
  const changedIndex = reference?.folderHint
    ? -1
    : resolvedPaths.findIndex((path) => !!path && !path.startsWith("/") && isChangedPath(path));
  const checking = !!openFile && changedIndex < 0 && serveable !== false;

  const listings = useQueries({
    queries: resolvedPaths.map((path) =>
      workspaceDirListingQuery(
        conversationId,
        path === null ? null : workspaceParentDirectory(path),
        checking,
      ),
    ),
  });

  // Walk candidates in order: the first found wins, but only once every
  // earlier candidate is known to be missing, so a link never switches target.
  let winner = changedIndex;
  let winnerKind: "file" | "folder" = "file";
  let undecided = false;
  if (winner < 0) {
    for (let index = 0; index < candidates.length; index += 1) {
      const resolution = resolutions[index];
      if (resolution === null) continue;
      if (resolution === "pending") {
        undecided = true;
        break;
      }
      const query = listings[index];
      let state = candidateState(query.isSuccess ? query.data : null, resolution.path);
      if (state === "missing" && missIsOutdated(query, renderedAt)) state = "pending";
      if (state === "pending") {
        undecided = true;
        break;
      }
      if (state === "missing") continue;
      winner = index;
      winnerKind = state;
      break;
    }
  }
  const anyResolved = resolvedPaths.some((path) => path !== null);
  const allMissing = winner < 0 && !undecided && anyResolved;

  // Fallback: a workspace search for a file whose path ends in the candidate.
  const searchable =
    !!reference && options.search !== false && allMissing && checking && worthSearching(reference);
  const searchTerms = searchable ? searchSuffixes(resolvedPaths) : [];
  const searchName = searchTerms.length ? baseName(searchTerms[0]) : "";
  const search = useQuery({
    queryKey: ["file-reference-search", conversationId, searchName],
    queryFn: ({ signal }) =>
      fetchWorkspaceFileSearch(conversationId!, searchName, "", "", "", signal),
    enabled: searchable && !!searchName && !!conversationId,
    // A cited file rarely moves mid-conversation; re-searching on every remount
    // (scrolling history) would rescan the workspace for each bare file name.
    staleTime: 5 * 60_000,
  });
  let searchMatch: string | null = null;
  let searchPending = false;
  if (searchable && searchName) {
    const result = search.data;
    if (!search.isSuccess || !result || result.unavailable) searchPending = true;
    else {
      const matches = result.files.filter(
        (file) =>
          file.type !== "directory" &&
          searchTerms.some((term) => file.path === term || file.path.endsWith(`/${term}`)),
      );
      if (matches.length === 1) searchMatch = matches[0].path;
      // A cut-short search that found nothing hasn't proven absence.
      else if (!matches.length && (result.truncated || missIsOutdated(search, renderedAt))) {
        searchPending = true;
      }
    }
  }

  // Re-check an outdated miss; a hit stays cached.
  const outdated = listings
    .map((query, index) => {
      const path = resolvedPaths[index];
      if (!checking || path === null || !query.isSuccess || query.isFetching) return false;
      return candidateState(query.data, path) === "missing" && missIsOutdated(query, renderedAt);
    })
    .join();
  const searchOutdated =
    searchable &&
    search.isSuccess &&
    !search.isFetching &&
    !searchMatch &&
    missIsOutdated(search, renderedAt);
  useEffect(() => {
    outdated.split(",").forEach((flag, index) => {
      if (flag === "true") void listings[index]?.refetch();
    });
    if (searchOutdated) void search.refetch();
  }, [outdated, searchOutdated, listings, search]);

  const citation =
    (winner >= 0 ? reference?.candidateCitations?.[winner] : undefined) ?? reference?.citation;
  const position: FilePosition | null =
    options.position ??
    (citation?.line
      ? {
          line: citation.line,
          ...(citation.column ? { column: citation.column } : {}),
          ...(citation.endLine ? { endLine: citation.endLine } : {}),
        }
      : null);

  const foundPath = winner >= 0 ? resolvedPaths[winner] : searchMatch;
  const firstPath = resolvedPaths.find((path) => path !== null) ?? null;
  const shownPath = foundPath ?? firstPath;
  const copyPath =
    shownPath === null
      ? (citation?.path ?? "")
      : (absoluteWorkspacePath(shownPath, root) ?? shownPath);
  const base = { path: shownPath ?? "", copyPath, candidateIndex: winner };

  if (!reference) return { ...base, status: "invalid", open: null };
  if (!openFile) return { ...base, status: "unavailable", open: null };

  if (foundPath) {
    if (winnerKind === "folder" && winner >= 0) {
      const absolute = absoluteWorkspacePath(foundPath, root);
      return {
        ...base,
        status: "folder",
        open: openFolder && absolute ? () => openFolder(absolute) : null,
      };
    }
    if (reference.folderHint) return { ...base, status: "missing", open: null };
    return {
      ...base,
      status: "file",
      open: () => (position ? openFile(foundPath, position) : openFile(foundPath)),
    };
  }
  if (undecided || searchPending) return { ...base, status: "pending", open: null };
  if (!anyResolved) return { ...base, status: "invalid", open: null };
  // Checks that never ran (no session, or a workspace that can't be served)
  // haven't verified anything.
  if (!checking) return { ...base, status: "pending", open: null };
  return { ...base, status: "missing", open: null };
}

/** True when a query's answer predates `renderedAt`, so its "not there" may be out of date. */
function missIsOutdated(query: { dataUpdatedAt: number }, renderedAt: number): boolean {
  return query.dataUpdatedAt < renderedAt;
}

/** Relative candidates a search result's path may end with (absolute and `~` ones never match). */
function searchSuffixes(paths: (string | null)[]): string[] {
  return paths.filter((path): path is string => !!path && !path.startsWith("/"));
}

function baseName(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}
