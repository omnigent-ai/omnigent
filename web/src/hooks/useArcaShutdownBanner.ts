import { useEffect, useReducer } from "react";
import { toast } from "sonner";
import { useHosts } from "@/hooks/useHosts";
import { useNowSelector } from "@/hooks/useNow";
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
  offersWorkweek,
  optOut,
} from "@/lib/arcaShutdownWarning";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";

export function useMarkArcaBannerWhenVisible(day: string) {
  useEffect(() => {
    const markIfVisible = () => {
      if (document.visibilityState !== "visible") return;
      markWarnedToday(new Date());
      toast.dismiss(`arca-shutdown:${day}`);
    };
    markIfVisible();
    document.addEventListener("visibilitychange", markIfVisible);
    return () => document.removeEventListener("visibilitychange", markIfVisible);
  }, [day]);
}

export function useArcaShutdownBanner() {
  const enabled = isFeatureEnabled(useServerInfo(), "arca_shutdown_warnings");
  const { data: hosts } = useHosts({ enabled });
  const warningDay = useNowSelector(
    (now) => (isWarningWindow(now) ? `${dateKey(now)}:${offersWorkweek(now) ? "1" : "0"}` : ""),
    { enabled },
  );
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
        warningDay &&
        !isOptedOut() &&
        !isDismissedToday(new Date()) &&
        hosts?.some(
          (host) =>
            host.host_id === hostId && host.status === "online" && isArcaHost(host, storedId),
        ),
      );
    },
    dismissToday: () => dismissToday(new Date()),
    optOut,
    day: warningDay.slice(0, 10),
    showWorkweek: warningDay.endsWith(":1"),
  };
}
