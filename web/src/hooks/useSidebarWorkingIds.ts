import { useEffect, useRef, useState } from "react";
import type { Conversation } from "./useConversations";

/** Hold idle transitions for grouping only; row badges still use live status. */
export function useSidebarWorkingIds(
  conversations: readonly Conversation[],
  startingId: string | null,
  enabled: boolean,
): ReadonlySet<string> {
  const [workingIds, setWorkingIds] = useState<ReadonlySet<string>>(() => new Set());
  // Infinity means still working; finite values are idle deadlines.
  const workingUntil = useRef(new Map<string, number>());

  useEffect(() => {
    const now = Date.now();
    const deadlines = new Map<string, number>();
    for (const conversation of enabled ? conversations : []) {
      const { id, status, pending_elicitations_count } = conversation;
      if (status === "failed" || (pending_elicitations_count ?? 0) > 0) continue;
      let until = workingUntil.current.get(id) ?? 0;
      if (status === "running" || id === startingId) until = Infinity;
      else if (until === Infinity) until = now + 10_000;
      if (until > now) deadlines.set(id, until);
    }
    workingUntil.current = deadlines;

    const publish = () => {
      const next = new Set(
        [...deadlines].filter(([, until]) => until > Date.now()).map(([id]) => id),
      );
      setWorkingIds((previous) =>
        next.size === previous.size && [...next].every((id) => previous.has(id)) ? previous : next,
      );
    };
    publish();
    const timers = [...deadlines.values()]
      .filter(Number.isFinite)
      .map((until) => setTimeout(publish, Math.max(0, until - Date.now())));
    return () => timers.forEach(clearTimeout);
  }, [conversations, startingId, enabled]);

  return workingIds;
}
