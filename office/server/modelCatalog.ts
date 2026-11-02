// Caches each host's model and effort choices per harness; a host probe can take seconds.

import {
  type HarnessModels,
  type ModelChoice,
  catalogHarness,
  effortNote,
  harnessEfforts,
  isUnlistedHarnessError,
  normalizeModelOptions,
} from "../shared/harnessModels.ts";

const LISTED_TTL_MS = 5 * 60_000;
const FAILED_TTL_MS = 15_000;

export interface ModelCatalogSources {
  /** Omnigent's host probe for a harness. */
  hostOptions: (hostId: string, harness: string) => Promise<unknown>;
  /** The harness CLI's own listing on this machine; null when it has none. */
  localModels?: (harness: string) => Promise<ModelChoice[] | null>;
  /** Whether the host runs on this machine, so a local CLI speaks for it. */
  isLocalHost?: (hostId: string) => Promise<boolean>;
  /** The harness's effort family from Omnigent's harness catalog. */
  effortFamily?: (harness: string) => Promise<string | null>;
  now?: () => number;
}

export class ModelCatalog {
  private readonly sources: ModelCatalogSources;
  private readonly now: () => number;
  private readonly entries = new Map<string, { at: number; value: Promise<HarnessModels>; failed: boolean }>();

  constructor(sources: ModelCatalogSources) {
    this.sources = sources;
    this.now = sources.now ?? Date.now;
  }

  models(hostId: string, harness: string): Promise<HarnessModels> {
    const key = `${hostId}\n${harness}`;
    const cached = this.entries.get(key);
    if (cached && this.now() - cached.at < (cached.failed ? FAILED_TTL_MS : LISTED_TTL_MS)) return cached.value;
    const entry = { at: this.now(), failed: false, value: this.resolve(hostId, harness) };
    void entry.value.then((result) => {
      entry.failed = Boolean(result.error);
    });
    this.entries.set(key, entry);
    return entry.value;
  }

  private async resolve(hostId: string, harness: string): Promise<HarnessModels> {
    const family = await (this.sources.effortFamily?.(harness) ?? Promise.resolve(null)).catch(() => null);
    const base = { efforts: harnessEfforts(harness, family), effortNote: effortNote(harness) };
    let error: string | null = null;
    try {
      const raw = await this.sources.hostOptions(hostId, catalogHarness(harness));
      const models = normalizeModelOptions(raw);
      const reported = (raw as { error?: unknown } | null)?.error;
      if (models.length || typeof reported !== "string" || !reported) return { ...base, listed: true, models, source: "host", error: null };
      error = reported;
    } catch (caught) {
      if (!isUnlistedHarnessError((caught as Error).message)) error = (caught as Error).message;
    }
    if (!error && this.sources.localModels && (await this.sources.isLocalHost?.(hostId).catch(() => false))) {
      const models = await this.sources.localModels(harness);
      if (models) return { ...base, listed: true, models, source: "local_cli", error: null };
    }
    return { ...base, listed: false, models: [], source: null, error };
  }
}
