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

/**
 * Desktop update toast.
 *
 * `variant` controls only the outer chrome so the SAME component serves two
 * hosts: `floating` (default) pins it bottom-right for the in-page web build;
 * `bare` drops the fixed positioning so the Electron shell can mount it inside
 * its own corner overlay window (which owns position/size). See
 * web/src/update-overlay.tsx + the shell's update overlay window.
 */
export function UpdateBanner({ variant = "floating" }: { variant?: "floating" | "bare" } = {}) {
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
  const isError = visibleStatus.state === "error-security";
  const Icon = isError
    ? AlertTriangleIcon
    : visibleStatus.state === "downloaded"
      ? RotateCcwIcon
      : DownloadIcon;

  return (
    <div
      role={isError ? "status" : "region"}
      aria-label="Desktop update"
      className={cn(
        "rounded-xl border border-border bg-background p-3.5 text-sm shadow-lg",
        // `floating`: pin bottom-right for the in-page web build. `bare`: fill
        // the shell's overlay window, which supplies position + size itself.
        variant === "floating"
          ? "fixed right-4 bottom-4 z-50 w-80 max-w-[calc(100vw-2rem)] animate-in fade-in-0 slide-in-from-bottom-2 duration-200"
          : "w-full",
      )}
    >
      <div className="flex items-start gap-3">
        <div
          className={cn(
            "mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full",
            isError
              ? "bg-amber-500/10 text-amber-600 dark:text-amber-500"
              : "bg-primary/10 text-primary",
          )}
        >
          <Icon className="size-4" aria-hidden="true" />
        </div>

        <div className="min-w-0 flex-1">
          {visibleStatus.state === "available" && (
            <p className="font-medium text-foreground">
              Omnigent {visibleStatus.info?.version ?? "update"} is available
            </p>
          )}
          {visibleStatus.state === "downloading" && (
            <>
              <p className="font-medium text-foreground">
                Downloading Omnigent update… {progress}%
              </p>
              <Progress
                value={progress}
                className="mt-2 h-1.5"
                aria-label="Update download progress"
              />
            </>
          )}
          {visibleStatus.state === "downloaded" && (
            <>
              <p className="font-medium text-foreground">
                Omnigent {visibleStatus.info?.version ?? "update"} is ready to install
              </p>
              {autoInstall && (
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Installs automatically on next quit.
                </p>
              )}
            </>
          )}
          {isError && (
            <>
              <p className="font-medium text-foreground">Update check failed</p>
              {visibleStatus.lastError && (
                <p className="mt-0.5 line-clamp-3 text-xs text-muted-foreground">
                  {visibleStatus.lastError}
                </p>
              )}
            </>
          )}

          {releaseNotes && visibleStatus.state !== "downloading" && (
            <details className="mt-1.5 text-xs text-muted-foreground">
              <summary className="cursor-pointer select-none text-foreground hover:underline">
                Release notes
              </summary>
              <div className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap">{releaseNotes}</div>
            </details>
          )}

          {visibleStatus.state === "available" && (
            <div className="mt-3 flex items-center gap-2">
              <Button
                size="sm"
                onClick={() => void onDownload()}
                loading={busyAction === "download"}
              >
                Update now
              </Button>
              <Button
                variant="outline"
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
            <div className="mt-3 flex items-center gap-2">
              <Button size="sm" onClick={() => void onInstall()} loading={busyAction === "install"}>
                Restart to update
              </Button>
            </div>
          )}
        </div>

        {(isError ||
          visibleStatus.state === "available" ||
          visibleStatus.state === "downloaded") && (
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Dismiss"
            className="-mt-1 -mr-1 shrink-0"
            onClick={() => setHiddenVersion(isError ? "error-security" : (version ?? null))}
          >
            <XIcon className="size-3.5" />
          </Button>
        )}
      </div>
    </div>
  );
}
