/** Read-only fleet quota status behind the LLMQ panel. */

import { authenticatedFetch } from "./identity";

/** The controller counts usage in parts per million of a window. */
const PPM = 1_000_000;

export interface QuotaWindow {
  provider: string;
  lane: string;
  limitId: string;
  windowName: string;
  modelScope: string;
  usedPpm: number;
  windowSeconds: number | null;
  resetsAt: number | null;
  /** 0 when the provider is refusing requests outright, 1 when allowing. */
  hardAllowed: number | null;
  source: string | null;
  observedAt: number | null;
  burstFactor: number | null;
}

export interface QuotaWorkstream {
  id: string;
  weight: number;
  explicitSharePpm: number | null;
  active: boolean;
  borrowAfterSeconds: number | null;
  lastSeenAt: number | null;
  activeReservations: number;
  activeEstimatedPpm: number;
  oldestActiveAgeSeconds: number | null;
}

export interface QuotaBurstPolicy {
  initialBurstFactor: number | null;
  maxBurstFactor: number | null;
  adaptiveEnabled: boolean | null;
}

export interface QuotaStatus {
  generatedAt: number;
  observedAt: number;
  windows: QuotaWindow[];
  workstreams: QuotaWorkstream[];
  burst: QuotaBurstPolicy;
  activeReservations: number;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function finiteOr(value: unknown, fallback: number): number {
  return numberOrNull(value) ?? fallback;
}

function parseWindow(raw: Record<string, unknown>): QuotaWindow {
  return {
    provider: String(raw.provider ?? "unknown"),
    lane: String(raw.lane ?? "unknown"),
    limitId: String(raw.limit_id ?? "unknown"),
    windowName: String(raw.window_name ?? "unknown"),
    modelScope: String(raw.model_scope ?? "*"),
    usedPpm: finiteOr(raw.used_ppm, 0),
    windowSeconds: numberOrNull(raw.window_seconds),
    resetsAt: numberOrNull(raw.resets_at),
    hardAllowed: numberOrNull(raw.hard_allowed),
    source: typeof raw.source === "string" ? raw.source : null,
    observedAt: numberOrNull(raw.observed_at),
    burstFactor: numberOrNull(raw.burst_factor),
  };
}

function parseWorkstream(raw: Record<string, unknown>): QuotaWorkstream {
  return {
    id: String(raw.id ?? ""),
    weight: finiteOr(raw.weight, 1),
    explicitSharePpm: numberOrNull(raw.explicit_share_ppm),
    active: Boolean(raw.active),
    borrowAfterSeconds: numberOrNull(raw.borrow_after_seconds),
    lastSeenAt: numberOrNull(raw.last_seen_at),
    activeReservations: finiteOr(raw.active_reservations, 0),
    activeEstimatedPpm: finiteOr(raw.active_estimated_ppm, 0),
    oldestActiveAgeSeconds: numberOrNull(raw.oldest_active_age_seconds),
  };
}

export function parseQuotaStatus(payload: unknown): QuotaStatus {
  if (typeof payload !== "object" || payload === null) {
    throw new Error("Invalid quota status response");
  }
  const raw = payload as Record<string, unknown>;
  const windows = Array.isArray(raw.windows) ? raw.windows : [];
  const workstreams = Array.isArray(raw.workstreams) ? raw.workstreams : [];
  const burst = (typeof raw.burst === "object" && raw.burst !== null ? raw.burst : {}) as Record<
    string,
    unknown
  >;
  return {
    generatedAt: finiteOr(raw.generated_at, 0),
    observedAt: finiteOr(raw.observed_at, 0),
    windows: windows
      .filter((row): row is Record<string, unknown> => typeof row === "object" && row !== null)
      .map(parseWindow),
    workstreams: workstreams
      .filter((row): row is Record<string, unknown> => typeof row === "object" && row !== null)
      .map(parseWorkstream)
      .filter((row) => row.id !== ""),
    burst: {
      initialBurstFactor: numberOrNull(burst.initial_burst_factor),
      maxBurstFactor: numberOrNull(burst.max_burst_factor),
      adaptiveEnabled: typeof burst.adaptive_enabled === "boolean" ? burst.adaptive_enabled : null,
    },
    activeReservations: finiteOr(raw.active_reservations, 0),
  };
}

export async function getQuotaStatus(signal?: AbortSignal): Promise<QuotaStatus> {
  const response = await authenticatedFetch("/v1/quota/status", signal ? { signal } : undefined);
  if (!response.ok) throw new Error(`Could not read quota status (${response.status})`);
  return parseQuotaStatus(await response.json());
}

/** Render a ppm usage value as a percentage of its window. */
export function formatUsedPercent(usedPpm: number): string {
  const percent = (usedPpm / PPM) * 100;
  // Sub-percent usage still reads as "0.3%" rather than a flat "0%", so a
  // window that has just started being consumed does not look untouched.
  return `${percent < 10 && percent > 0 ? percent.toFixed(1) : Math.round(percent)}%`;
}

/** A window's fill as a 0-1 fraction, clamped for use as a bar width. */
export function usedFraction(usedPpm: number): number {
  return Math.min(1, Math.max(0, usedPpm / PPM));
}

/** Compact duration, e.g. "45s", "12m", "3h 20m", "2d 4h". */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainderMinutes = minutes % 60;
  if (hours < 24) return remainderMinutes ? `${hours}h ${remainderMinutes}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const remainderHours = hours % 24;
  return remainderHours ? `${days}d ${remainderHours}h` : `${days}d`;
}

/** "in 3h 20m" for a future reset, "due" once the reset time has passed. */
export function formatResetsIn(resetsAt: number | null, now: number): string | null {
  if (resetsAt === null) return null;
  const remaining = resetsAt - now;
  return remaining <= 0 ? "due" : `in ${formatDuration(remaining)}`;
}

/**
 * The window's label, collapsing the controller's separate scope field when it
 * carries no information ("*" means "every model on this lane").
 */
export function formatWindowScope(window: QuotaWindow): string {
  return window.modelScope === "*" ? window.limitId : `${window.limitId} · ${window.modelScope}`;
}

/**
 * A workstream's configured share. An explicit share is authoritative; without
 * one the controller derives the share from weights, so show the weight and say
 * so rather than implying a fixed percentage.
 */
export function formatShare(
  workstream: QuotaWorkstream,
  workstreams: readonly QuotaWorkstream[],
): string {
  if (workstream.explicitSharePpm !== null) {
    return formatUsedPercent(workstream.explicitSharePpm);
  }
  const weighted = workstreams.filter((row) => row.explicitSharePpm === null && row.active);
  const totalWeight = weighted.reduce((sum, row) => sum + row.weight, 0);
  if (!workstream.active || totalWeight <= 0) return `weight ${workstream.weight}`;
  const explicit = workstreams.reduce((sum, row) => sum + (row.explicitSharePpm ?? 0), 0);
  const remaining = Math.max(0, PPM - explicit);
  return `~${formatUsedPercent((remaining * workstream.weight) / totalWeight)}`;
}
