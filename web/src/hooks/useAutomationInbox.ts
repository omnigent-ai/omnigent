import { useInfiniteQuery } from "@tanstack/react-query";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { withBasePath } from "@/lib/basePath";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";
import { useRebasePath } from "@/lib/routing";
import { listAutomationFailures } from "@/lib/scheduledTasksApi";
import { useViewerId } from "./useViewerId";
import { useSeenAutomationFailures } from "./useSeenAutomationFailures";

export function useAutomationInbox(identityReady: boolean) {
  const info = useServerInfo();
  const viewerId = useViewerId();
  const rebasePath = useRebasePath();
  const server = getOmnigentServerIdentity();
  const scope = JSON.stringify([
    server ?? `mount:${getOmnigentHostGeneration()}`,
    withBasePath(rebasePath("/")),
    viewerId,
  ]);
  const enabled =
    identityReady && viewerId !== null && info !== "loading" && info.scheduled_task_inbox === true;
  const { seen, markRead, markManyRead, unmarkRead } = useSeenAutomationFailures(
    scope,
    server !== null,
  );
  const query = useInfiniteQuery({
    queryKey: ["automation-inbox", scope],
    queryFn: ({ pageParam }) => listAutomationFailures(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor ?? undefined,
    enabled,
    staleTime: 60_000,
    // A small summary feed; keep global polling slower than the Automations page.
    refetchInterval: 300_000,
    retry: 1,
  });
  const failures = enabled ? (query.data?.pages.flatMap((page) => page.data) ?? []) : [];
  const unique = new Map(failures.map((item) => [item.taskId, item]));
  return {
    ...query,
    isLoading: enabled && query.isLoading,
    isError: enabled && query.isError,
    hasNextPage: enabled && query.hasNextPage,
    items: [...unique.values()].filter((item) => !seen.has(item.runId)),
    markRead,
    markManyRead,
    unmarkRead,
  };
}
