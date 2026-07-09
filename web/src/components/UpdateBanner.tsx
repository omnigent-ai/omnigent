import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangleIcon, DownloadIcon, RotateCcwIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { type UpdateStatus, updateBridge } from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";

function statusVersion(status: UpdateStatus | null): string | null {
  return status?.info?.version ?? null;
}

function formatPercent(percent: number | undefined): number {
  if (typeof percent !== "number" || !Number.isFinite(percent)) return 0;
  return Math.max(0, Math.min(100, Math.round(percent)));
}

export function UpdateBanner() {
  const bridge = updateBridge();
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [skippedVersion, setSkippedVersion] = useState<string | null | "loading">("loading");
  const [autoInstall, setAutoInstall] = useState(true);
  const [hiddenVersion, setHiddenVersion] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<"download" | "install" | "skip" | null>(null);

  useEffect(() => {
    if (!bridge) return undefined;
    let alive = true;
    let unsubscribe: (() => void) | undefined;

    void bridge
      .getStatus()
      .then((currentStatus) => {
        if (!alive) return;
        setStatus(currentStatus);
        unsubscribe = bridge.onStatus((nextStatus) => {
          setStatus(nextStatus);
        });
        void bridge
          .getConfig()
          .then((config) => {
            if (alive) {
              setSkippedVersion(config.skippedVersion);
              setAutoInstall(config.autoInstall);
            }
          })
          .catch((err) => {
            console.warn("[UpdateBanner] update bridge config read failed:", err);
            if (alive) setSkippedVersion(null);
          });
      })
      .catch((err) => {
        console.warn("[UpdateBanner] update bridge status read failed:", err);
        if (alive) setSkippedVersion(null);
      });

    return () => {
      alive = false;
      unsubscribe?.();
    };
  }, [bridge]);

  const version = statusVersion(status);
  const hidden =
    skippedVersion === "loading" ||
    (version !== null && (version === skippedVersion || version === hiddenVersion)) ||
    (status?.state === "error-security" && hiddenVersion === "error-security");
  const visibleStatus = useMemo(() => {
    if (!status || hidden) return null;
    if (status.state === "idle" || status.state === "checking" || status.state === "none") {
      return null;
    }
    return status;
  }, [hidden, status]);

  const onDownload = useCallback(async () => {
    if (!bridge) return;
    setBusyAction("download");
    try {
      await bridge.download();
    } finally {
      setBusyAction(null);
    }
  }, [bridge]);

  const onInstall = useCallback(async () => {
    if (!bridge) return;
    setBusyAction("install");
    try {
      await bridge.installNow();
    } finally {
      setBusyAction(null);
    }
  }, [bridge]);

  const onSkip = useCallback(async () => {
    if (!bridge || !version) return;
    setBusyAction("skip");
    try {
      const next = await bridge.setConfig({ skippedVersion: version });
      setSkippedVersion(next.skippedVersion);
    } finally {
      setBusyAction(null);
    }
  }, [bridge, version]);

  if (!bridge || !visibleStatus) return null;

  const releaseNotes = visibleStatus.info?.releaseNotes;
  const progress =
    visibleStatus.state === "downloading" ? formatPercent(visibleStatus.progress?.percent) : 0;

  return (
    <div
      role={visibleStatus.state === "error-security" ? "status" : "region"}
      aria-label="Desktop update"
      className={cn(
        "border-b border-border bg-background/95 px-4 py-2 shadow-sm backdrop-blur",
        visibleStatus.state === "error-security" && "bg-muted/70",
      )}
    >
      <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-3 gap-y-2 text-sm">
        {visibleStatus.state === "error-security" ? (
          <AlertTriangleIcon className="size-4 text-muted-foreground" aria-hidden="true" />
        ) : visibleStatus.state === "downloaded" ? (
          <RotateCcwIcon className="size-4 text-primary" aria-hidden="true" />
        ) : (
          <DownloadIcon className="size-4 text-primary" aria-hidden="true" />
        )}

        <div className="min-w-0 flex-1">
          {visibleStatus.state === "available" && (
            <span>Omnigent {visibleStatus.info?.version ?? "update"} is available.</span>
          )}
          {visibleStatus.state === "downloading" && (
            <div className="flex flex-col gap-1">
              <span>Downloading Omnigent update… {progress}%</span>
              <Progress
                value={progress}
                className="h-1.5 max-w-80"
                aria-label="Update download progress"
              />
            </div>
          )}
          {visibleStatus.state === "downloaded" && (
            <span>Omnigent {visibleStatus.info?.version ?? "update"} is ready to install.</span>
          )}
          {visibleStatus.state === "error-security" && (
            <span>
              Last update check failed
              {visibleStatus.lastError ? `: ${visibleStatus.lastError}` : "."}
            </span>
          )}
        </div>

        {visibleStatus.state === "available" && (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={() => void onDownload()} loading={busyAction === "download"}>
              Update now
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setHiddenVersion(version)}>
              Later
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void onSkip()}
              loading={busyAction === "skip"}
              disabled={!version}
            >
              Skip this version
            </Button>
          </div>
        )}

        {visibleStatus.state === "downloaded" && (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={() => void onInstall()} loading={busyAction === "install"}>
              Restart to update
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setHiddenVersion(version)}>
              {autoInstall ? "Later — install on next quit" : "Later"}
            </Button>
          </div>
        )}

        {releaseNotes && visibleStatus.state !== "downloading" && (
          <details className="basis-full text-xs text-muted-foreground">
            <summary className="cursor-pointer select-none text-foreground">Release notes</summary>
            <div className="mt-1 whitespace-pre-wrap">{releaseNotes}</div>
          </details>
        )}

        {visibleStatus.state === "error-security" && (
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Dismiss update warning"
            onClick={() => setHiddenVersion(version ?? "error-security")}
          >
            <XIcon className="size-3.5" />
          </Button>
        )}
      </div>
    </div>
  );
}
