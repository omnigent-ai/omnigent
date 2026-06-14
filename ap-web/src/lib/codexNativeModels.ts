/**
 * Codex-native model picker options. Keep this list aligned with the
 * subscription-tier model catalog Omnigent exposes for Codex-backed agents.
 *
 * Lives in a leaf module (no React / store imports) so both the picker UI
 * (`ChatPage`) and the store (`chatStore`) can read it without a circular
 * import.
 */
export const CODEX_NATIVE_MODELS = [
  // Ordered by capability tier, most powerful first.
  { id: "gpt-5.5", label: "GPT-5.5" },
  { id: "gpt-5.4", label: "GPT-5.4" },
  { id: "gpt-5.4-mini", label: "GPT-5.4 mini" },
] as const;

/**
 * Is `model` something a Codex-native session can actually run?
 *
 * Accepts the advertised picker ids plus the common OpenAI/Codex naming
 * families Omnigent can receive from gateway-backed model ids. Rejects
 * Claude aliases so cross-harness sticky picks are not applied to Codex.
 *
 * @param model - A model id / alias, or null/undefined.
 * @returns True only for a Codex-compatible model.
 */
export function isCodexNativeModel(model: string | null | undefined): boolean {
  if (model == null) return false;
  const id = model.toLowerCase();
  return (
    CODEX_NATIVE_MODELS.some((m) => m.id === id) ||
    id.startsWith("gpt-") ||
    id.startsWith("databricks-gpt-") ||
    id.includes("codex")
  );
}
