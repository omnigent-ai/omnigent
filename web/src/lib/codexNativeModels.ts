import type { NativeModelOption } from "./types";

/** Catalog prefixes a gateway model id carries, mirroring the server's fold. */
const CATALOG_PREFIXES = ["databricks-", "system.ai."] as const;

/**
 * Fold a model id to the spelling Codex row ids compare in.
 *
 * Comparison only, never a value to send anywhere: Codex spells versions with
 * dots (``gpt-5.6-luna``) where the catalog spells them with dashes
 * (``databricks-gpt-5-6-luna``), and Codex's bundled rows carry its own
 * spelling in both ``id`` and ``model``. Mirrors the server's
 * ``comparable_model_id``.
 */
function comparableModelId(model: string | null | undefined): string {
  // Null-safe: a picker row may carry a null ``model`` (cursor rows have
  // only ``id`` + ``displayName``), and folding it must not throw. An empty
  // fold never equals a real (non-empty) target, so it simply never matches.
  const raw = model?.trim();
  if (!raw) return "";
  let bare = raw.toLowerCase();
  if (bare.endsWith("[1m]")) bare = bare.slice(0, -"[1m]".length);
  for (const prefix of CATALOG_PREFIXES) {
    if (bare.startsWith(prefix)) {
      bare = bare.slice(prefix.length);
      break;
    }
  }
  return bare.replaceAll(".", "-");
}

/**
 * Find a native picker option by its UI alias or provider-facing model id.
 *
 * Falls back to comparing folded spellings so a session bound to a catalog id
 * still resolves to the Codex row naming the same model.
 *
 * @param options - Native model options from the session snapshot.
 * @param model - Candidate model id, e.g. ``"gpt-5.5"``.
 * @returns The matching option, or ``null`` when unknown.
 */
export function findNativeModelOption(
  options: readonly NativeModelOption[],
  model: string | null | undefined,
): NativeModelOption | null {
  const raw = model?.trim();
  if (!raw) return null;
  const exact = options.find((option) => option.id === raw || option.model === raw);
  if (exact !== undefined) return exact;
  const target = comparableModelId(raw);
  return (
    options.find(
      (option) =>
        (option.id != null && comparableModelId(option.id) === target) ||
        (option.model != null && comparableModelId(option.model) === target),
    ) ?? null
  );
}

/**
 * Whether a sticky model id is one Codex advertised for this session.
 *
 * @param options - Codex model options from the session snapshot.
 * @param model - Candidate model id.
 * @returns True only when the candidate matches a Codex-returned option.
 */
export function isCodexNativeModel(
  options: readonly NativeModelOption[],
  model: string | null | undefined,
): boolean {
  return findNativeModelOption(options, model) !== null;
}

/**
 * Effort levels for the currently selected Codex model.
 *
 * Codex's ``isDefault`` is a static property of its bundled catalog, not the
 * model a session launched with, so it cannot stand in for an unresolved
 * model — it would offer levels the running model rejects. An unknown model
 * yields no levels and the caller hides the picker until the model resolves.
 *
 * @param options - Codex model options from the session snapshot.
 * @param currentModel - Active override or bound model id.
 * @returns Model-specific effort values from Codex ``model/list``.
 */
export function codexEffortLevelsForModel(
  options: readonly NativeModelOption[],
  currentModel: string | null | undefined,
): readonly string[] {
  const selected = findNativeModelOption(options, currentModel);
  if (selected === null) return [];
  const efforts = selected.supportedReasoningEfforts ?? [];
  return Array.from(
    new Set(
      efforts
        .map((option) => option.reasoningEffort)
        .filter((effort): effort is string => typeof effort === "string" && effort.length > 0),
    ),
  );
}
