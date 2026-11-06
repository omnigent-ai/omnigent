import { useMutation, useQueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

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
 *   destination folder already exists or a 422 when git fails.
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
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as CloneHostRepositoryResponse;
  return { path: body.path, name: body.name, reusedExisting: body.reused_existing };
}

/**
 * React Query mutation for {@link cloneHostRepository}. Refreshes that host's
 * cached folder listings so the new checkout shows up in the folder picker.
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
    onSuccess: (_cloned, { hostId }) => {
      void queryClient.invalidateQueries({ queryKey: ["host-filesystem", hostId] });
    },
  });
}
