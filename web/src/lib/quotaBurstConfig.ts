/** Persisted UI-side quota burst policy and its controller integration seam. */

import { authenticatedFetch } from "./identity";

export const QUOTA_BURST_CONFIG_STORAGE_KEY = "omnigent:quota-burst-config";
export const QUOTA_BURST_MIN = 1;
export const QUOTA_BURST_MAX = 8;
export const QUOTA_BURST_STEP = 0.25;
export const QUOTA_BURST_FINITE_DEFAULT = 2;

export interface QuotaBurstConfig {
  /** Null means there is no configured ceiling. */
  maxBurstFactor: number | null;
  /** Let observed duty cycle select the effective factor below the ceiling. */
  adaptiveBurstEnabled: boolean;
}

export const DEFAULT_QUOTA_BURST_CONFIG: QuotaBurstConfig = {
  maxBurstFactor: null,
  adaptiveBurstEnabled: true,
};

export function normalizeQuotaBurstFactor(value: number): number {
  if (!Number.isFinite(value)) return QUOTA_BURST_FINITE_DEFAULT;
  const clamped = Math.min(QUOTA_BURST_MAX, Math.max(QUOTA_BURST_MIN, value));
  return Math.round(clamped / QUOTA_BURST_STEP) * QUOTA_BURST_STEP;
}

export function readQuotaBurstConfig(): QuotaBurstConfig {
  if (typeof window === "undefined") return DEFAULT_QUOTA_BURST_CONFIG;
  try {
    const raw = window.localStorage.getItem(QUOTA_BURST_CONFIG_STORAGE_KEY);
    if (!raw) return DEFAULT_QUOTA_BURST_CONFIG;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const maxBurstFactor =
      parsed.max_burst_factor === null
        ? null
        : typeof parsed.max_burst_factor === "number" &&
            Number.isFinite(parsed.max_burst_factor) &&
            parsed.max_burst_factor >= QUOTA_BURST_MIN
          ? parsed.max_burst_factor
          : DEFAULT_QUOTA_BURST_CONFIG.maxBurstFactor;
    return {
      maxBurstFactor,
      adaptiveBurstEnabled:
        typeof parsed.adaptive_burst_enabled === "boolean"
          ? parsed.adaptive_burst_enabled
          : DEFAULT_QUOTA_BURST_CONFIG.adaptiveBurstEnabled,
    };
  } catch {
    return DEFAULT_QUOTA_BURST_CONFIG;
  }
}

export type QuotaBurstConfigWriter = (config: QuotaBurstConfig) => void | Promise<void>;

function persistQuotaBurstConfig(config: QuotaBurstConfig): void {
  try {
    window.localStorage.setItem(
      QUOTA_BURST_CONFIG_STORAGE_KEY,
      JSON.stringify({
        max_burst_factor: config.maxBurstFactor,
        adaptive_burst_enabled: config.adaptiveBurstEnabled,
      }),
    );
  } catch {
    // Local storage is a fallback cache, never required for a live update.
  }
}

export async function getQuotaBurstControllerConfig(): Promise<QuotaBurstConfig> {
  const response = await authenticatedFetch("/v1/quota/config");
  if (!response.ok) throw new Error(`Could not read quota burst config (${response.status})`);
  const payload = (await response.json()) as Record<string, unknown>;
  const rawFactor = payload.max_burst_factor;
  if (
    !(rawFactor === null || (typeof rawFactor === "number" && Number.isFinite(rawFactor))) ||
    typeof payload.adaptive_enabled !== "boolean"
  ) {
    throw new Error("Invalid quota burst config response");
  }
  if (typeof rawFactor === "number" && rawFactor < QUOTA_BURST_MIN) {
    throw new Error("Invalid quota burst config response");
  }
  const config = {
    maxBurstFactor: rawFactor,
    adaptiveBurstEnabled: payload.adaptive_enabled,
  };
  persistQuotaBurstConfig(config);
  return config;
}

export async function updateQuotaBurstController(config: QuotaBurstConfig): Promise<void> {
  const response = await authenticatedFetch("/v1/quota/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      max_burst_factor: config.maxBurstFactor,
      adaptive_enabled: config.adaptiveBurstEnabled,
    }),
  });
  if (!response.ok) throw new Error(`Could not update quota burst config (${response.status})`);
}

let controllerWriter: QuotaBurstConfigWriter | null = updateQuotaBurstController;
let controllerWriteTail: Promise<void> = Promise.resolve();

/** Install the transport that updates the quota controller when one is available. */
export function setQuotaBurstConfigWriter(writer: QuotaBurstConfigWriter | null): void {
  controllerWriter = writer;
}

export async function writeQuotaBurstConfig(config: QuotaBurstConfig): Promise<void> {
  const normalized: QuotaBurstConfig = {
    maxBurstFactor: config.maxBurstFactor,
    adaptiveBurstEnabled: config.adaptiveBurstEnabled,
  };
  const operation = controllerWriteTail
    .catch(() => undefined)
    .then(async () => {
      await controllerWriter?.(normalized);
      persistQuotaBurstConfig(normalized);
    });
  controllerWriteTail = operation;
  await operation;
}
