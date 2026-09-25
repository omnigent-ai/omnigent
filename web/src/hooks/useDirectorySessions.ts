import { useMemo } from "react";
import { useSidebarData } from "./useSidebarData";

// Best-effort directory warnings use loaded owned sessions, including pagination.
// Reading this cache never requests more sessions or activates Shared.
export function useDirectorySessions(enabled: boolean) {
  const { mine } = useSidebarData();
  const data = useMemo(
    () => (enabled ? mine.data?.pages.flatMap((page) => page.data) : undefined),
    [enabled, mine.data],
  );
  return { data };
}
