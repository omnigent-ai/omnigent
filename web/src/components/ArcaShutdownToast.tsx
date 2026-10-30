import { useEffect, useReducer, useRef, useState } from "react";
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
  isWarnedToday,
  isWarningWindow,
  markWarnedToday,
} from "@/lib/arcaShutdownWarning";
import { copyText } from "@/lib/clipboard";
import { ArcaExtendActions, useCanExtendArca } from "./ArcaExtendActions";

const OVERNIGHT_COMMAND = "arca extend overnight";

export function ArcaShutdownToast() {
  const { data: hosts } = useHosts();
  const now = useNow();
  const desktopExtend = useCanExtendArca();
  const [visibility, setVisibility] = useState(() => document.visibilityState);
  const [, refresh] = useReducer((value: number) => value + 1, 0);
  const shownToastId = useRef<string | null>(null);
  const storedId = readArcaHostId();
  const hasOnlineArcaHost = hosts?.some(
    (host) => host.status === "online" && isArcaHost(host, storedId),
  );
  const dismissedToday = isDismissedToday(now);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (isArcaWarningStorageKey(event.key)) refresh();
    };
    const onVisibilityChange = () => setVisibility(document.visibilityState);
    window.addEventListener(ARCA_WARNING_PREFERENCES_CHANGED, refresh);
    window.addEventListener("storage", onStorage);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener(ARCA_WARNING_PREFERENCES_CHANGED, refresh);
      window.removeEventListener("storage", onStorage);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, []);

  useEffect(() => {
    const id = `arca-shutdown:${dateKey(now)}`;
    if (shownToastId.current && shownToastId.current !== id) {
      toast.dismiss(shownToastId.current);
      shownToastId.current = null;
    }
    if (!isWarningWindow(now) || !hasOnlineArcaHost || dismissedToday) {
      if (shownToastId.current) toast.dismiss(shownToastId.current);
      shownToastId.current = null;
      return;
    }
    if (visibility !== "visible" || isWarnedToday(now) || desktopExtend === null) return;

    markWarnedToday(now);
    shownToastId.current = id;
    toast("Arca shuts down at about 6 PM", {
      id,
      description: desktopExtend ? (
        <ArcaExtendActions
          day={dateKey(now)}
          onSuccess={() => dismissToday(new Date())}
          fallback={(mode) => {
            const command = `arca extend ${mode}`;
            return (
              <button
                type="button"
                onClick={() =>
                  void copyText(command).then(
                    () => toast.success("Copied"),
                    () => toast.error("Could not copy command"),
                  )
                }
              >
                Copy {command}
              </button>
            );
          }}
        />
      ) : (
        <span>
          Run <code>{OVERNIGHT_COMMAND}</code> on your laptop to keep it running.
        </span>
      ),
      duration: Infinity,
      closeButton: true,
      // Sonner's unlayered display and gap rules need these overrides.
      classNames: {
        toast: "!grid !grid-cols-[max-content_minmax(0,1fr)] !gap-2",
        content: "col-span-2",
        cancelButton: "!m-0",
        actionButton: "!m-0 justify-self-start",
      },
      action: desktopExtend
        ? undefined
        : {
            label: "Copy command",
            onClick: (event) => {
              event.preventDefault();
              void copyText(OVERNIGHT_COMMAND).then(
                () => {
                  toast.dismiss(id);
                  toast.success("Copied");
                },
                () => toast.error("Could not copy command"),
              );
            },
          },
      cancel: { label: "Not now", onClick: () => dismissToday(new Date()) },
    });
  }, [now, hasOnlineArcaHost, dismissedToday, visibility, desktopExtend]);

  return null;
}
