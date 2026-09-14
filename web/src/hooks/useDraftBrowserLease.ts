import { useEffect } from "react";
import { renewDraftBrowserLease, supportsBrowser } from "@/lib/nativeBridge";

export const DRAFT_BROWSER_HEARTBEAT_INTERVAL_MS = 60_000;

export function useDraftBrowserLease(workspaceId: string, active: boolean): void {
  useEffect(() => {
    if (!active || !workspaceId.startsWith("draft-workspace:") || !supportsBrowser()) return;
    const renew = () => void renewDraftBrowserLease(workspaceId);
    renew();
    const timer = window.setInterval(renew, DRAFT_BROWSER_HEARTBEAT_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [active, workspaceId]);
}
