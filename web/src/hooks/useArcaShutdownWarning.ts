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

export function useArcaShutdownWarning(
  showToast = false,
  activeHostId: string | null = null,
  activeHostLoading = false,
) {
  const info = useServerInfo();
  const enabled = isFeatureEnabled(info, "arca_shutdown_warnings");
  const { data: hosts } = useHosts({ enabled });
  const now = useNow();
  const [, refresh] = useReducer((value: number) => value + 1, 0);
  const storedId = readArcaHostId();
  const onlineArcaHosts = hosts?.filter(
    (host) => host.status === "online" && isArcaHost(host, storedId),
  );
  const hasOnlineArcaHost = Boolean(onlineArcaHosts?.length);
  const activeArcaHostOnline = onlineArcaHosts?.some((host) => host.host_id === activeHostId);

  useEffect(() => {
    if (!showToast || !enabled || activeHostLoading || !isWarningWindow(now) || !hasOnlineArcaHost)
      return;
    if (isOptedOut() || isToastedToday(now)) return;
    markToastedToday(now);
    if (!isDismissedToday(now) && activeArcaHostOnline) return;
    toast("Arca shuts down at about 6 PM", {
      id: `arca-shutdown:${dateKey(now)}`,
      description: createElement(
        "span",
        null,
        "Run ",
        createElement("code", null, OVERNIGHT_COMMAND),
        " on your laptop to keep it running.",
      ),
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
  }, [showToast, enabled, now, hasOnlineArcaHost, activeArcaHostOnline, activeHostLoading]);

  useEffect(() => {
    window.addEventListener(ARCA_WARNING_PREFERENCES_CHANGED, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(ARCA_WARNING_PREFERENCES_CHANGED, refresh);
      window.removeEventListener("storage", refresh);
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
