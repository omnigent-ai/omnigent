import { createElement, useEffect, useReducer } from "react";
import { toast } from "sonner";
import { useHosts } from "@/hooks/useHosts";
import { useNow } from "@/hooks/useNow";
import { readArcaHostId } from "@/lib/arcaHost";
import {
  ARCA_WARNING_PREFERENCES_CHANGED,
  dateKey,
  dismissToday as storeDismissToday,
  isArcaHost,
  isDismissedToday,
  isToastedToday,
  isOptedOut,
  isWarningWindow,
  markToastedToday,
  optOut as storeOptOut,
} from "@/lib/arcaShutdownWarning";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";

const OVERNIGHT_COMMAND = "arca extend overnight";
const pendingToastTicks = new WeakSet<Date>();

export function useMarkArcaWarningWhenVisible(now: Date) {
  useEffect(() => {
    const markIfVisible = () => {
      if (document.visibilityState === "visible") markToastedToday(now);
    };
    markIfVisible();
    document.addEventListener("visibilitychange", markIfVisible);
    return () => document.removeEventListener("visibilitychange", markIfVisible);
  }, [now]);
}

function ToastDescription({ now }: { now: Date }) {
  useMarkArcaWarningWhenVisible(now);

  return createElement(
    "span",
    null,
    "Run ",
    createElement("code", null, OVERNIGHT_COMMAND),
    " on your laptop to keep it running.",
  );
}

export function useArcaShutdownWarning(
  showToast = false,
  activeHostId: string | null = null,
  activeHostLoading = false,
) {
  const info = useServerInfo();
  const enabled = isFeatureEnabled(info, "arca_shutdown_warnings");
  const { data: hosts } = useHosts({ enabled });
  const now = useNow();
  const [refreshTick, refresh] = useReducer((value: number) => value + 1, 0);
  const storedId = readArcaHostId();
  const onlineArcaHosts = hosts?.filter(
    (host) => host.status === "online" && isArcaHost(host, storedId),
  );
  const hasOnlineArcaHost = Boolean(onlineArcaHosts?.length);
  const activeArcaHostOnline = onlineArcaHosts?.some((host) => host.host_id === activeHostId);

  useEffect(() => {
    if (
      !showToast ||
      !enabled ||
      activeHostLoading ||
      document.visibilityState !== "visible" ||
      !isWarningWindow(now) ||
      !hasOnlineArcaHost
    )
      return;
    if (isOptedOut() || isDismissedToday(now) || isToastedToday(now) || activeArcaHostOnline)
      return;
    const day = dateKey(now);
    if (pendingToastTicks.has(now)) return;
    pendingToastTicks.add(now);
    toast("Arca shuts down at about 6 PM", {
      id: `arca-shutdown:${day}`,
      description: createElement(ToastDescription, { now }),
      duration: Infinity,
      closeButton: true,
      classNames: {
        toast: "!grid !grid-cols-[max-content_minmax(0,1fr)] !gap-2",
        content: "!col-span-2 !min-w-0",
        cancelButton: "!col-start-1 !row-start-2 !m-0",
        actionButton: "!col-start-2 !row-start-2 !m-0 !justify-self-start",
      },
      action: {
        label: "Copy command",
        onClick: () => {
          void navigator.clipboard
            .writeText(OVERNIGHT_COMMAND)
            .then(() => toast.success("Copied"))
            .catch(() => undefined);
        },
      },
      cancel: { label: "Not now", onClick: () => storeDismissToday(now) },
    });
  }, [
    showToast,
    enabled,
    now,
    hasOnlineArcaHost,
    activeArcaHostOnline,
    activeHostLoading,
    refreshTick,
  ]);

  useEffect(() => {
    window.addEventListener(ARCA_WARNING_PREFERENCES_CHANGED, refresh);
    window.addEventListener("storage", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener(ARCA_WARNING_PREFERENCES_CHANGED, refresh);
      window.removeEventListener("storage", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  return {
    showForHost(hostId: string | null | undefined): boolean {
      return Boolean(
        enabled &&
        hostId &&
        isWarningWindow(now) &&
        !isOptedOut() &&
        !isDismissedToday(now) &&
        onlineArcaHosts?.some((host) => host.host_id === hostId),
      );
    },
    dismissToday(): void {
      storeDismissToday(now);
    },
    optOut(): void {
      storeOptOut();
    },
    now,
  };
}
