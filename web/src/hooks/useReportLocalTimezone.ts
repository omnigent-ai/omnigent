import { useEffect } from "react";
import { useIdentityReady, useViewerId } from "@/hooks/useViewerId";
import { reportLocalTimezone } from "@/lib/scheduledTasksApi";

/**
 * Once identity settles, record the browser's zone as the viewer's automation
 * default. A server without auth has no viewer id but still records it as local.
 */
export function useReportLocalTimezone(): void {
  const identityReady = useIdentityReady();
  const viewerId = useViewerId();
  useEffect(() => {
    if (identityReady) void reportLocalTimezone();
  }, [identityReady, viewerId]);
}
