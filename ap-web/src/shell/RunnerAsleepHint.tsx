import { PauseCircleIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

/**
 * Shown in a workspace panel when the session's runner is bound but not
 * connected (HTTP 503 → {@link RunnerOfflineError}). For host-bound
 * sessions this recovers as soon as the user sends a message (the host
 * relaunches the runner), so we guide them there instead of surfacing a
 * raw "Failed to load: 503". Shared by the Changed and All file tabs so
 * both read identically.
 */
export function RunnerAsleepHint() {
  const { t } = useTranslation("common");
  return (
    <div className="flex flex-col items-start gap-1 px-2 py-1.5 text-muted-foreground text-xs">
      <span className="flex items-center gap-1.5 font-medium text-foreground">
        <PauseCircleIcon className="size-3.5 shrink-0" />
        {t("agentAsleep")}
      </span>
      <span>{t("sendMessageToReconnect")}</span>
    </div>
  );
}
