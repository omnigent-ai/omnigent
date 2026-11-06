import { useMutation, useQueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/identity";
import { ApiError, apiErrorFromResponse } from "@/lib/sessionsApi";
import { isNavigablePath } from "@/shell/WorkspacePicker";

/** The route's own 404 detail; any other 404 means the route itself is missing. */
const HOST_NOT_FOUND_DETAIL = "host not found";

/** Shown when the server predates the clone route, e.g. after a rollback. */
export const CLONE_UNSUPPORTED_MESSAGE =
  "This Omnigent server can't clone repositories yet. Update the server, or clone the " +
  "repository on the host and choose its folder instead.";

/** A checkout returned by ``POST /v1/hosts/{id}/repositories``. */
export interface ClonedHostRepository {
  /** Absolute checkout path on the host, e.g. ``"/Users/me/code/repo"``. */
  path: string;
  /** Repository folder name derived from the URL, e.g. ``"repo"``. */
  name: string;
  /** True when the folder already held a checkout of the same remote. */
  reusedExisting: boolean;
}

interface CloneHostRepositoryResponse {
  object: "repository_clone";
  path: string;
  name: string;
  reused_existing: boolean;
}

/**
 * Clone a git repository onto a host with the host user's credentials.
 *
 * The request stays open until git finishes, which can take minutes for a
 * large repository.
 *
 * @param hostId Host to clone onto.
 * @param url ``https://`` or ``git@`` repository URL.
 * @param parentDirectory Absolute or ``~`` folder that will hold the checkout.
 * @returns The checkout's path and name.
 * @throws ApiError carrying the server's reason, e.g. a 409 when the
 *   destination folder already exists or a 422 when git fails, or
 *   {@link CLONE_UNSUPPORTED_MESSAGE} when the server has no clone route.
 */
export async function cloneHostRepository(
  hostId: string,
  url: string,
  parentDirectory: string,
): Promise<ClonedHostRepository> {
  const res = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}/repositories`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: url.trim(), parent_directory: parentDirectory.trim() }),
  });
  if (!res.ok) {
    const error = await apiErrorFromResponse(res);
    const routeMissing =
      res.status === 405 || (res.status === 404 && error.message !== HOST_NOT_FOUND_DETAIL);
    if (routeMissing) throw new ApiError(CLONE_UNSUPPORTED_MESSAGE, res.status, error.code);
    throw error;
  }
  const body = (await res.json()) as CloneHostRepositoryResponse;
  return { path: body.path, name: body.name, reusedExisting: body.reused_existing };
}

const CLONE_PARENT_STORAGE_KEY = "omnigent:clone-repository-parent";
const DEFAULT_CLONE_PARENT = "~";

/**
 * Folder the user last cloned into on ``hostId``, or ``~``.
 *
 * @param hostId Host the clone targets.
 * @returns A ``~`` or absolute folder, e.g. ``"~/code"``.
 */
export function readLastCloneParent(hostId: string | null): string {
  if (hostId === null) return DEFAULT_CLONE_PARENT;
  try {
    const parent = readStoredCloneParents()[hostId];
    return typeof parent === "string" && isNavigablePath(parent) ? parent : DEFAULT_CLONE_PARENT;
  } catch {
    return DEFAULT_CLONE_PARENT;
  }
}

/**
 * The remembered per-host clone folders, or ``{}`` when storage holds anything
 * other than a JSON object.
 *
 * @returns Stored values keyed by host id.
 * @throws When storage is unavailable.
 */
function readStoredCloneParents(): Record<string, unknown> {
  const raw = localStorage.getItem(CLONE_PARENT_STORAGE_KEY);
  let stored: unknown;
  try {
    stored = JSON.parse(raw ?? "{}");
  } catch {
    return {};
  }
  return typeof stored === "object" && stored !== null && !Array.isArray(stored)
    ? (stored as Record<string, unknown>)
    : {};
}

/**
 * Remember ``parent`` as the default clone folder for ``hostId``.
 *
 * @param hostId Host the clone targeted.
 * @param parent Folder that held the new checkout.
 */
export function rememberCloneParent(hostId: string, parent: string): void {
  try {
    const stored = readStoredCloneParents();
    localStorage.setItem(
      CLONE_PARENT_STORAGE_KEY,
      JSON.stringify({ ...stored, [hostId]: parent.trim() }),
    );
  } catch {
    // Storage can be full or disabled; the default folder still works.
  }
}

/**
 * React Query mutation for {@link cloneHostRepository}. Refreshes that host's
 * cached folder listings and remembers the folder, even when the form that
 * started the clone has unmounted.
 *
 * @returns A mutation; call ``mutateAsync({ hostId, url, parentDirectory })``.
 */
export function useCloneHostRepository() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      hostId,
      url,
      parentDirectory,
    }: {
      hostId: string;
      url: string;
      parentDirectory: string;
    }) => cloneHostRepository(hostId, url, parentDirectory),
    onSuccess: (_cloned, { hostId, parentDirectory }) => {
      rememberCloneParent(hostId, parentDirectory);
      void queryClient.invalidateQueries({ queryKey: ["host-filesystem", hostId] });
    },
  });
}
