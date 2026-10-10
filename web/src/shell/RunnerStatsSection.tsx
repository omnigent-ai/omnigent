import type { ReactNode } from "react";
import type { Host } from "@/hooks/useHosts";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";
import { formatBytes } from "./fileStatusUtils";

const GIB = 1024 ** 3;

/**
 * Whether the tooltip shows the runner section: readings, or an offline host's
 * last-seen. The server only sends readings while fresh by its own clock.
 */
export function hasRunnerStats(host: Host | undefined): boolean {
  if (!host?.stats) return false;
  return host.status === "offline" || Object.keys(host.stats).some((k) => k !== "reported_at");
}

// Used-percent thresholds at which a meter turns amber, then red.
const WARN_PERCENT = 80;
const CRITICAL_PERCENT = 95;

type MeterTone = "normal" | "warn" | "critical";

const TONE_FILL: Record<MeterTone, string> = {
  normal: "bg-primary",
  warn: "bg-warning",
  critical: "bg-destructive",
};

function meterTone(usedPercent: number): MeterTone {
  if (usedPercent >= CRITICAL_PERCENT) return "critical";
  if (usedPercent >= WARN_PERCENT) return "warn";
  return "normal";
}

/** Seconds under a minute ("8s ago"), then the sidebar's compact units ("3m ago"). */
function snapshotAge(reportedAtS: number, nowMs: number): string {
  const ageMs = Math.max(0, nowMs - reportedAtS * 1000);
  if (ageMs < 60_000) return `${Math.floor(ageMs / 1000)}s ago`;
  return `${relativeTime(reportedAtS * 1000, nowMs)} ago`;
}

/** One shared unit, e.g. "11.2 / 16 GB". */
function memoryLabel(usedBytes: number, totalBytes: number): string {
  return `${(usedBytes / GIB).toFixed(1)} / ${Number((totalBytes / GIB).toFixed(1))} GB`;
}

function MeterRow({
  label,
  usedPercent,
  value,
}: {
  label: string;
  usedPercent: number;
  value: string;
}) {
  const clamped = Math.min(100, Math.max(0, usedPercent));
  const tone = meterTone(clamped);
  return (
    <>
      <span className="text-muted-foreground">{label}</span>
      {/* Same track as ui/Progress, but a real meter: a usage gauge, not a task. */}
      <div
        role="meter"
        aria-label={`${label} used`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(clamped)}
        aria-valuetext={value}
        data-tone={tone}
        className="h-1 w-full overflow-hidden rounded-full bg-muted"
      >
        <div
          className={cn("h-full rounded-full", TONE_FILL[tone])}
          style={{ width: `${clamped}%` }}
        />
      </div>
      <span className="text-right whitespace-nowrap tabular-nums">{value}</span>
    </>
  );
}

/**
 * Host resource section for the sidebar session tooltip: a header naming the
 * host with its status and snapshot age, then CPU, Memory and Disk meters
 * (filled by the used fraction) and current network throughput.
 *
 * Renders only from the `Host` the sidebar's existing hosts query delivered —
 * nothing is fetched on hover. The caller gates on the `host_stats` release
 * feature and {@link hasRunnerStats}. A host that disconnected after reporting
 * arrives with only `reported_at`, and shows "offline · last seen …" with no
 * meters; a host that never reported (older build) renders nothing.
 */
export function RunnerStatsSection({ host, label = host.name }: { host: Host; label?: string }) {
  const stats = host.stats;
  if (!stats) return null;
  const online = host.status === "online";
  const age = snapshotAge(stats.reported_at, Date.now());

  const rows: ReactNode[] = [];
  if (online) {
    const {
      cpu_percent: cpu,
      memory_total_bytes: memTotal,
      memory_used_bytes: memUsed,
      disk_total_bytes: diskTotal,
      disk_free_bytes: diskFree,
      net_rx_bytes_per_s: rx,
      net_tx_bytes_per_s: tx,
    } = stats;
    if (cpu !== undefined) {
      rows.push(<MeterRow key="cpu" label="CPU" usedPercent={cpu} value={`${Math.round(cpu)}%`} />);
    }
    if (memTotal && memUsed !== undefined) {
      rows.push(
        <MeterRow
          key="memory"
          label="Memory"
          usedPercent={(memUsed / memTotal) * 100}
          value={memoryLabel(memUsed, memTotal)}
        />,
      );
    }
    if (diskTotal && diskFree !== undefined) {
      rows.push(
        <MeterRow
          key="disk"
          label="Disk"
          usedPercent={((diskTotal - diskFree) / diskTotal) * 100}
          value={`${formatBytes(diskFree)} free / ${formatBytes(diskTotal)}`}
        />,
      );
    }
    if (rx !== undefined && tx !== undefined) {
      rows.push(
        <span key="network-label" className="text-muted-foreground">
          Network
        </span>,
        <span key="network" className="col-span-2 flex gap-3 whitespace-nowrap tabular-nums">
          <span>
            <span aria-hidden>↓ </span>
            <span className="sr-only">download </span>
            {formatBytes(rx)}/s
          </span>
          <span>
            <span aria-hidden>↑ </span>
            <span className="sr-only">upload </span>
            {formatBytes(tx)}/s
          </span>
        </span>,
      );
    }
  }

  return (
    <div
      data-testid="runner-stats-section"
      className="mt-2 border-t border-foreground/10 pt-2 text-xs"
    >
      <p
        data-testid="runner-stats-header"
        className="flex items-center gap-1.5 text-muted-foreground"
      >
        <span className="shrink-0 text-[10px] font-medium tracking-wide uppercase">Runner</span>
        <span className="truncate text-popover-foreground">{label}</span>
        <span className="shrink-0">
          · {online ? "online" : "offline"} · {online ? age : `last seen ${age}`}
        </span>
      </p>
      {rows.length > 0 && (
        <div className="mt-1.5 grid grid-cols-[auto_minmax(2rem,1fr)_auto] items-center gap-x-2 gap-y-1">
          {rows}
        </div>
      )}
    </div>
  );
}
