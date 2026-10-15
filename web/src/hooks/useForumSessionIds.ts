import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { forumRequest } from "@/lib/forumsApi";

/** Include older forums whose parent sessions predate the Forum label. */
export function useForumSessionIds() {
  const enabled = isFeatureEnabled(useServerInfo(), "forums");
  const { data } = useQuery({
    queryKey: ["forum-session-ids"],
    enabled,
    queryFn: async () => {
      const ids: string[] = [];
      let cursor: string | null = null;
      do {
        // eslint-disable-next-line no-await-in-loop
        const page: { data: string[]; next_cursor: string | null } = await forumRequest(
          `?session_ids_only=true${cursor ? `&after=${encodeURIComponent(cursor)}` : ""}`,
        );
        ids.push(...page.data);
        cursor = page.next_cursor;
      } while (cursor);
      return ids;
    },
    staleTime: 10000,
    refetchInterval: 10000,
  });
  return useMemo(() => new Set(data ?? []), [data]);
}

export function isForumSession(
  session: { id: string; labels?: Record<string, string> },
  forumIds: ReadonlySet<string>,
) {
  return (
    forumIds.has(session.id) ||
    !!session.labels?.["omnigent.forum"] ||
    !!session.labels?.["omnigent.forum.parent"]
  );
}
