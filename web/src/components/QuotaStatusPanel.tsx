/** Fleet-wide LLMQ status: provider windows and workstream buckets. */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  formatDuration,
  formatResetsIn,
  formatShare,
  formatUsedPercent,
  formatWindowScope,
  getQuotaStatus,
  usedFraction,
  type QuotaStatus,
  type QuotaWindow,
  type QuotaWorkstream,
} from "@/lib/quotaStatus";

/** Live enough to watch a burst change take effect, idle enough to ignore. */
const REFRESH_INTERVAL_MS = 15_000;

/** Fill levels at which a window stops being routine. */
const WINDOW_WARN_FRACTION = 0.75;
const WINDOW_CRITICAL_FRACTION = 0.9;

function barToneFor(window: QuotaWindow): string {
  if (window.hardAllowed === 0) return "bg-destructive";
  const fraction = usedFraction(window.usedPpm);
  if (fraction >= WINDOW_CRITICAL_FRACTION) return "bg-destructive";
  if (fraction >= WINDOW_WARN_FRACTION) return "bg-amber-500";
  return "bg-primary";
}

function WindowRow({ window: quotaWindow, now }: { window: QuotaWindow; now: number }) {
  const resets = formatResetsIn(quotaWindow.resetsAt, now);
  const blocked = quotaWindow.hardAllowed === 0;
  return (
    <div className="flex flex-col gap-1" data-testid="quota-window-row">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="min-w-0 truncate text-sm">
          <span className="font-medium">{quotaWindow.provider}</span>
          <span className="text-muted-foreground"> · {formatWindowScope(quotaWindow)}</span>
        </span>
        <span className="flex items-baseline gap-2 text-sm tabular-nums">
          {quotaWindow.burstFactor !== null && (
            <span className="text-muted-foreground" title="Live burst factor for this window">
              {quotaWindow.burstFactor.toFixed(2)}×
            </span>
          )}
          <span className={blocked ? "text-destructive" : undefined}>
            {blocked ? "blocked" : formatUsedPercent(quotaWindow.usedPpm)}
          </span>
        </span>
      </div>
      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
        role="progressbar"
        aria-label={`${quotaWindow.provider} ${quotaWindow.windowName} window`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(usedFraction(quotaWindow.usedPpm) * 100)}
      >
        <div
          className={`h-full rounded-full ${barToneFor(quotaWindow)}`}
          style={{ width: `${usedFraction(quotaWindow.usedPpm) * 100}%` }}
        />
      </div>
      <div className="flex flex-wrap gap-x-3 text-xs text-muted-foreground">
        <span>
          {quotaWindow.windowSeconds !== null
            ? `${formatDuration(quotaWindow.windowSeconds)} window`
            : "no window"}
        </span>
        {resets && <span>resets {resets}</span>}
        <span className="truncate">{quotaWindow.lane}</span>
      </div>
    </div>
  );
}

function WorkstreamRow({
  workstream,
  workstreams,
}: {
  workstream: QuotaWorkstream;
  workstreams: readonly QuotaWorkstream[];
}) {
  // A reservation older than the bucket's borrow threshold is the signal that
  // this bucket is being held back rather than simply working slowly.
  const waiting =
    workstream.borrowAfterSeconds !== null &&
    workstream.oldestActiveAgeSeconds !== null &&
    workstream.oldestActiveAgeSeconds >= workstream.borrowAfterSeconds;
  return (
    <div
      className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5"
      data-testid="quota-workstream-row"
    >
      <span className="min-w-0 flex-1 truncate text-sm">
        <span className={workstream.active ? undefined : "text-muted-foreground"}>
          {workstream.id}
        </span>
        {!workstream.active && <span className="text-xs text-muted-foreground"> (idle)</span>}
      </span>
      <span className="flex items-baseline gap-3 text-xs tabular-nums text-muted-foreground">
        <span title="Configured share of the fleet">{formatShare(workstream, workstreams)}</span>
        <span className="w-20 text-right" title="Reservations the controller is holding open">
          {workstream.activeReservations === 0 ? "—" : `${workstream.activeReservations} in flight`}
        </span>
        <span
          className={`w-16 text-right ${waiting ? "text-amber-600 dark:text-amber-500" : ""}`}
          title="Age of the oldest still-open reservation"
        >
          {workstream.oldestActiveAgeSeconds === null
            ? "—"
            : formatDuration(workstream.oldestActiveAgeSeconds)}
        </span>
      </span>
    </div>
  );
}

/**
 * Fleet quota status. Polls rather than streaming: the controller publishes no
 * change feed, and at a 15s cadence the panel is live enough to watch a burst
 * change land without holding a connection open per viewer.
 */
export function QuotaStatusPanel() {
  const [status, setStatus] = useState<QuotaStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const inFlight = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;
    try {
      const next = await getQuotaStatus(controller.signal);
      if (controller.signal.aborted) return;
      setStatus(next);
      setNow(next.observedAt);
      setError(null);
    } catch {
      if (controller.signal.aborted) return;
      // Keep the last good reading on screen; a stale number beats an empty
      // panel when the controller blips.
      setError("Quota controller unavailable.");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    return () => {
      window.clearInterval(timer);
      inFlight.current?.abort();
    };
  }, [refresh]);

  if (!status) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        {error ?? "Loading quota status…"}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-4" data-testid="quota-status-panel">
      <div className="flex flex-col gap-2">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-ui font-medium">Provider windows</span>
          <span className="text-xs text-muted-foreground tabular-nums">
            {status.activeReservations} in flight
          </span>
        </div>
        {status.windows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            The controller is not tracking any provider windows.
          </p>
        ) : (
          <div className="flex flex-col gap-3">
            {status.windows.map((quotaWindow) => (
              <WindowRow
                key={`${quotaWindow.provider}|${quotaWindow.lane}|${quotaWindow.limitId}|${quotaWindow.windowName}|${quotaWindow.modelScope}`}
                window={quotaWindow}
                now={now}
              />
            ))}
          </div>
        )}
      </div>

      <div className="flex flex-col gap-2 border-t border-border pt-4">
        <span className="text-ui font-medium">Workstreams</span>
        {status.workstreams.length === 0 ? (
          <p className="text-sm text-muted-foreground">No workstreams are registered.</p>
        ) : (
          <div className="flex flex-col gap-1.5">
            {status.workstreams.map((workstream) => (
              <WorkstreamRow
                key={workstream.id}
                workstream={workstream}
                workstreams={status.workstreams}
              />
            ))}
          </div>
        )}
      </div>

      {error && (
        <p role="status" className="text-sm text-muted-foreground">
          {error} Showing the last reading.
        </p>
      )}
    </div>
  );
}
