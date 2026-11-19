/**
 * Who made a model, as opposed to who serves it: Claude through Bedrock is
 * Anthropic, Kimi through Databricks is Moonshot AI. The model pickers group and
 * search large catalogs by this creator.
 *
 * Catalog rows carry no creator field, so it comes from the row's own ids: a
 * `<creator>/<model>` namespace (OpenRouter, Hugging Face, Together, NIM), a
 * Bedrock `<creator>.<model>` prefix, then the model-name prefixes below. These
 * are not routing or harness-compatibility rules; unknown ids stay selectable
 * under {@link OTHER_MODELS}.
 */
import type { NativeModelLabelFields } from "@/lib/composerModelLabel";

export interface ModelCreator {
  id: string;
  label: string;
}

interface CreatorRule extends ModelCreator {
  namespaces: readonly string[];
  models?: RegExp;
}

// Display order. Name patterns run against the lowercased model name once host
// prefixes (`databricks-`, `system.ai.`, Bedrock regions) are removed.
const CREATOR_RULES: readonly CreatorRule[] = [
  {
    id: "anthropic",
    label: "Anthropic",
    namespaces: ["anthropic"],
    models: /^(?:claude|(?:opus|sonnet|haiku|fable)(?:$|[-\d[]))/,
  },
  {
    id: "openai",
    label: "OpenAI",
    namespaces: ["openai"],
    models: /^(?:gpt|chatgpt|codex|o\d+(?:$|-)|text-embedding|whisper)/,
  },
  {
    id: "google",
    label: "Google",
    namespaces: ["google"],
    models: /^(?:gemini|gemma|codegemma|learnlm)/,
  },
  { id: "xai", label: "xAI", namespaces: ["xai", "x-ai"], models: /^grok/ },
  {
    id: "moonshotai",
    label: "Moonshot AI",
    namespaces: ["moonshotai", "moonshot", "moonshot-ai"],
    models: /^(?:kimi|moonshot)/,
  },
  {
    id: "zai",
    label: "Z.ai",
    namespaces: ["zai", "z-ai", "zai-org", "zhipuai", "zhipu", "thudm"],
    models: /^(?:zai-)?(?:glm|chatglm|autoglm|codegeex)/,
  },
  {
    id: "alibaba",
    label: "Alibaba / Qwen",
    namespaces: ["alibaba", "qwen", "dashscope"],
    models: /^(?:qwen|qwq|qvq)/,
  },
  {
    id: "meta",
    label: "Meta",
    namespaces: ["meta", "meta-llama", "facebook"],
    // NVIDIA's Nemotron fine-tunes keep the Llama name.
    models: /^(?:(?:meta-|code)?llama(?!.*nemotron)|muse-spark)/,
  },
  {
    id: "deepseek",
    label: "DeepSeek",
    namespaces: ["deepseek", "deepseek-ai"],
    models: /^deepseek/,
  },
  {
    id: "mistral",
    label: "Mistral AI",
    namespaces: ["mistral", "mistralai"],
    models: /^(?:open-)?(?:mistral|mixtral|codestral|devstral|magistral|ministral|pixtral|voxtral)/,
  },
  { id: "minimax", label: "MiniMax", namespaces: ["minimax", "minimaxai"], models: /^minimax/ },
  { id: "amazon", label: "Amazon", namespaces: ["amazon"], models: /^(?:nova|titan)-/ },
  {
    id: "cohere",
    label: "Cohere",
    namespaces: ["cohere", "cohereforai", "coherelabs"],
    models: /^(?:command|c4ai|aya)/,
  },
  { id: "microsoft", label: "Microsoft", namespaces: ["microsoft"], models: /^(?:phi-?\d|mai-)/ },
  { id: "nvidia", label: "NVIDIA", namespaces: ["nvidia"], models: /nemotron/ },
  {
    id: "bytedance",
    label: "ByteDance",
    namespaces: ["bytedance", "bytedance-seed"],
    models: /^(?:doubao|seed-)/,
  },
  { id: "xiaomi", label: "Xiaomi", namespaces: ["xiaomi", "xiaomimimo"], models: /^mimo/ },
  { id: "perplexity", label: "Perplexity", namespaces: ["perplexity"], models: /^sonar/ },
  { id: "ibm", label: "IBM", namespaces: ["ibm", "ibm-granite"], models: /^granite/ },
];

export const OTHER_MODELS: ModelCreator = { id: "other", label: "Other models" };

/** Catalogs at least this long get search, and creator groups when they span creators. */
export const MODEL_CATALOG_BROWSE_MIN = 10;

const CREATOR_BY_NAMESPACE = new Map(
  CREATOR_RULES.flatMap((rule) => rule.namespaces.map((namespace) => [namespace, rule] as const)),
);
const CREATOR_ORDER = new Map(CREATOR_RULES.map((rule, index) => [rule.id, index]));
// Path segments that are neither a host nor an organization, e.g. Fireworks'
// `accounts/fireworks/models/<model>` and Gemini's `models/<model>`.
const PATH_SEGMENTS = new Set(["accounts", "models"]);
const SERVING_PREFIX = /^(?:databricks-|system\.ai\.)/;
const BEDROCK_REGION_PREFIX = /^(?:us|us-gov|eu|apac|jp|au|ca|in|global)\./;

function creatorForReference(reference: string): CreatorRule | undefined {
  const segments = reference
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-")
    .split("/")
    .map((segment) => segment.replace(/^[~@]/, ""))
    .filter((segment) => segment !== "" && !PATH_SEGMENTS.has(segment));
  const name = segments.pop();
  if (!name) return undefined;
  const local = name.replace(SERVING_PREFIX, "").replace(BEDROCK_REGION_PREFIX, "");
  const vendor = local.includes(".")
    ? CREATOR_BY_NAMESPACE.get(local.slice(0, local.indexOf(".")))
    : undefined;
  if (vendor) return vendor;
  const namespace = CREATOR_BY_NAMESPACE.get(segments.at(-1) ?? "");
  // Under a host (`openrouter/<org>/<model>`) the organization published the
  // model, even a fine-tune named after its base. A lone leading namespace is
  // usually the serving provider (Pi's `mistral/zai-glm-5-2`), so the name wins.
  if (namespace && segments.length > 1) return namespace;
  return CREATOR_RULES.find((rule) => rule.models?.test(local)) ?? namespace;
}

/** The creator of a catalog row, or {@link OTHER_MODELS} when its ids don't identify one. */
export function modelCreator(option: NativeModelLabelFields): ModelCreator {
  const references = [option.model, option.id];
  // Under an organization (`host/<org>/<model>`) a display name may name the
  // base model of that organization's fine-tune; otherwise it can label an alias.
  if (!references.some((reference) => (reference?.split("/").length ?? 0) > 2)) {
    references.push(option.displayName);
  }
  for (const reference of references) {
    const rule = reference ? creatorForReference(reference) : undefined;
    if (rule) return { id: rule.id, label: rule.label };
  }
  return OTHER_MODELS;
}

export interface ModelCreatorGroup<T> {
  creator: ModelCreator;
  items: T[];
}

/** Bucket items by creator in display order, with Other models last; items keep catalog order. */
export function groupByModelCreator<T>(
  items: readonly T[],
  optionOf: (item: T) => NativeModelLabelFields,
): ModelCreatorGroup<T>[] {
  const groups = new Map<string, ModelCreatorGroup<T>>();
  for (const item of items) {
    const creator = modelCreator(optionOf(item));
    const group = groups.get(creator.id) ?? { creator, items: [] };
    group.items.push(item);
    groups.set(creator.id, group);
  }
  const rank = (group: ModelCreatorGroup<T>) =>
    CREATOR_ORDER.get(group.creator.id) ?? CREATOR_RULES.length;
  return [...groups.values()].sort((left, right) => rank(left) - rank(right));
}

/** Whether every whitespace-separated term of `query` occurs in the row's ids, label, or creator. */
export function matchesModelSearch(
  option: NativeModelLabelFields,
  label: string,
  query: string,
): boolean {
  const creator = modelCreator(option);
  const haystack = [option.id, option.model, label, creator.id, creator.label]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((term) => haystack.includes(term));
}
