import type { QueryClient } from "@tanstack/react-query";
import { fetchHosts } from "@/hooks/useHosts";

/** Online host ids, fetched fresh, to tell a newly connected host apart later. */
export async function fetchOnlineHostIds(queryClient: QueryClient): Promise<Set<string>> {
  const hosts = await queryClient.fetchQuery({
    queryKey: ["hosts", { includeSandbox: false }],
    queryFn: () => fetchHosts(false),
    staleTime: 0,
  });
  return new Set(hosts.filter((host) => host.status === "online").map((host) => host.host_id));
}

/**
 * Poll the host list for a host that wasn't online before a connect started it.
 * For connects that started a host but couldn't read its id.
 *
 * @returns The new host's id, or null when none came online in time.
 */
export async function waitForNewOnlineHost(
  queryClient: QueryClient,
  onlineBefore: ReadonlySet<string>,
  timeoutMs = 30_000,
): Promise<string | null> {
  const deadline = Date.now() + timeoutMs;
  // Sequential by design: each poll must see the previous one's result.
  /* oxlint-disable no-await-in-loop */
  while (Date.now() < deadline) {
    const online = await fetchOnlineHostIds(queryClient);
    const fresh = [...online].find((hostId) => !onlineBefore.has(hostId));
    if (fresh) return fresh;
    await new Promise((resolve) => {
      setTimeout(resolve, 1500);
    });
  }
  /* oxlint-enable no-await-in-loop */
  return null;
}
