// Model and effort choices per harness, read from the host's own model probe
// (`GET /v1/hosts/{id}/harnesses/{harness}/model-options`) or, for harnesses
// Omnigent cannot list, from the harness CLI on this machine.

export interface ModelChoice {
  /** The exact id a session launch takes, e.g. "opus" or "gpt-6.1-sol". */
  id: string;
  label: string;
  isDefault: boolean;
  /** Efforts this model supports, when the harness reports them per model. */
  efforts: string[] | null;
  defaultEffort: string | null;
}

export interface HarnessModels {
  /** False when no model list is available; a model id may still be typed. */
  listed: boolean;
  models: ModelChoice[];
  /** Reasoning efforts the harness accepts; empty when it takes none. */
  efforts: string[];
  /** Where the list came from: the host's probe or the harness CLI on this machine. */
  source: "host" | "local_cli" | null;
  /** Shown when efforts are empty but the harness still has a notion of effort. */
  effortNote: string | null;
  error: string | null;
}

export const EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] as const;

// Mirrors omnigent/util/reasoning_effort.py: the vocabulary each effort family accepts.
const FAMILY_EFFORTS: Record<string, readonly string[]> = {
  anthropic: ["low", "medium", "high", "xhigh", "max"],
  openai: ["none", "minimal", "low", "medium", "high", "xhigh"],
  gemini: ["low", "medium", "high"],
  copilot: ["low", "medium", "high", "xhigh"],
  pi: EFFORT_ORDER,
  "codex-native": EFFORT_ORDER,
};

// Effort families of native harnesses, which `/v1/harnesses` does not list as rows.
const NATIVE_EFFORT_FAMILY: Record<string, string> = {
  "claude-native": "anthropic",
  "codex-native": "codex-native",
  "pi-native": "pi",
};

// agy picks effort through the model variant (e.g. "gemini-4-argon-high"), not a separate setting.
const EFFORT_IN_MODEL: Record<string, string> = {
  "antigravity-native": "Effort is part of the model choice (Low, Medium, or High variant).",
};

// SDK harnesses that run the same CLI, and so take the same model ids, as a probed native harness.
const CATALOG_HARNESS: Record<string, string> = {
  codex: "codex-native",
};

/** The harness whose model probe answers for `harness`. */
export function catalogHarness(harness: string): string {
  return CATALOG_HARNESS[harness] ?? harness;
}

/** Efforts a harness accepts, from its catalog effort family or the native fallback. */
export function harnessEfforts(harness: string, effortFamily: string | null | undefined): string[] {
  if (EFFORT_IN_MODEL[harness]) return [];
  const family = effortFamily ?? NATIVE_EFFORT_FAMILY[harness];
  return family ? [...(FAMILY_EFFORTS[family] ?? [])] : [];
}

export function effortNote(harness: string): string | null {
  return EFFORT_IN_MODEL[harness] ?? null;
}

/** Efforts to offer for a model: its own list within the harness's vocabulary. */
export function modelEfforts(harnessLevels: string[], model: ModelChoice | undefined): string[] {
  if (!model?.efforts?.length) return harnessLevels;
  const supported = new Set(model.efforts);
  const narrowed = harnessLevels.filter((level) => supported.has(level));
  return narrowed.length ? narrowed : harnessLevels;
}

/** Omnigent's reply when a harness has no model probe, as opposed to a probe that failed. */
export function isUnlistedHarnessError(message: string): boolean {
  return /model options are unsupported/i.test(message);
}

/** Picker rows from a model-options response; hidden and malformed rows are dropped. */
export function normalizeModelOptions(raw: unknown): ModelChoice[] {
  const models = (raw as { models?: unknown } | null)?.models;
  if (!Array.isArray(models)) return [];
  const seen = new Set<string>();
  const choices: ModelChoice[] = [];
  for (const entry of models) {
    if (!entry || typeof entry !== "object") continue;
    const row = entry as Record<string, unknown>;
    if (typeof row.id !== "string" || !row.id || row.hidden === true || seen.has(row.id)) continue;
    seen.add(row.id);
    const name = typeof row.displayName === "string" && row.displayName.trim() ? row.displayName.trim() : row.id;
    const model = typeof row.model === "string" && row.model !== row.id ? row.model : null;
    const efforts = Array.isArray(row.supportedReasoningEfforts)
      ? row.supportedReasoningEfforts
          .map((effort) => (effort as { reasoningEffort?: unknown } | null)?.reasoningEffort)
          .filter((effort): effort is string => typeof effort === "string")
      : [];
    choices.push({
      id: row.id,
      label: model && name !== model ? `${name} (${model})` : name,
      isDefault: row.isDefault === true,
      efforts: efforts.length ? efforts : null,
      defaultEffort: typeof row.defaultReasoningEffort === "string" ? row.defaultReasoningEffort : null,
    });
  }
  return choices;
}

function choice(id: string, label: string): ModelChoice {
  return { id, label, isDefault: false, efforts: null, defaultEffort: null };
}

/** Rows of `agy models`: "<id>\t<display name>". */
export function parseAgyModels(output: string): ModelChoice[] {
  return output
    .split("\n")
    .map((line) => /^([a-z0-9][\w.:-]*)\t+(.+)$/i.exec(line.trim()))
    .filter((match): match is RegExpExecArray => match !== null)
    .map(([, id, label]) => choice(id, label.trim()));
}

/** The model ids `copilot help config` lists under its `model` setting. */
export function parseCopilotConfigModels(output: string): ModelChoice[] {
  const lines = output.split("\n");
  const start = lines.findIndex((line) => /^\s*`model`:/.test(line));
  if (start < 0) return [];
  const models: ModelChoice[] = [];
  for (const line of lines.slice(start + 1)) {
    const match = /^\s*-\s*"([^"]+)"\s*$/.exec(line);
    if (!match) {
      if (models.length) break;
      continue;
    }
    models.push(choice(match[1], match[1]));
  }
  return models;
}
