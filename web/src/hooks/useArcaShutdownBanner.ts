import { useEffect, useReducer } from "react";
import { toast } from "sonner";
import { useHosts } from "@/hooks/useHosts";
import { useNow } from "@/hooks/useNow";
import { isArcaHost, readArcaHostId } from "@/lib/arcaHost";
import {
  ARCA_WARNING_PREFERENCES_CHANGED,
  dateKey,
  dismissToday,
  isArcaWarningStorageKey,
  isDismissedToday,
  isOptedOut,
  isWarningWindow,
  markWarnedToday,
  optOut,
} from "@/lib/arcaShutdownWarning";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";

export function useMarkArcaBannerWhenVisible(now: Date) {
  useEffect(() => {
    const markIfVisible = () => {
      if (document.visibilityState !== "visible") return;
      markWarnedToday(now);
      toast.dismiss(`arca-shutdown:${dateKey(now)}`);
    };
    markIfVisible();
    document.addEventListener("visibilitychange", markIfVisible);
    return () => document.removeEventListener("visibilitychange", markIfVisible);
  }, [now]);
}

export function useArcaShutdownBanner() {
  const enabled = isFeatureEnabled(useServerInfo(), "arca_shutdown_warnings");
  const { data: hosts } = useHosts({ enabled });
  const now = useNow();
  const [, refresh] = useReducer((value: number) => value + 1, 0);
  const storedId = readArcaHostId();

  useEffect(() => {
    if (!enabled) return;
    const onStorage = (event: StorageEvent) => {
      if (isArcaWarningStorageKey(event.key)) refresh();
    };
    window.addEventListener(ARCA_WARNING_PREFERENCES_CHANGED, refresh);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(ARCA_WARNING_PREFERENCES_CHANGED, refresh);
      window.removeEventListener("storage", onStorage);
    };
  }, [enabled]);

  return {
    showForHost(hostId: string | null | undefined): boolean {
      return Boolean(
        enabled &&
        hostId &&
        isWarningWindow(now) &&
        !isOptedOut() &&
        !isDismissedToday(now) &&
        hosts?.some(
          (host) =>
            host.host_id === hostId && host.status === "online" && isArcaHost(host, storedId),
        ),
      );
    },
    dismissToday: () => dismissToday(now),
    optOut,
    now,
  };
}
