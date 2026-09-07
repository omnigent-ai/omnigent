const CODEX_NATIVE_WRAPPER = "codex-native-ui";
// Mirrors the server's collaboration-mode label. Seeded at create by the
// new-session Plan-mode control so the runner launches the fresh Codex
// thread already in Plan mode; kept current in-session by the PATCH path.
export const CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY = "omnigent.codex_native.collaboration_mode";

export type CodexPlanModeLabelSource =
  { labels?: Record<string, string | null> | null } | null | undefined;

export function isCodexNativeSession(source: CodexPlanModeLabelSource): boolean {
  return source?.labels?.["omnigent.wrapper"] === CODEX_NATIVE_WRAPPER;
}

export function codexPlanModeFromLabels(
  labels: Record<string, string | null> | null | undefined,
): boolean {
  return labels?.[CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY] === "plan";
}

export function codexPlanModeFromSession(source: CodexPlanModeLabelSource): boolean {
  return isCodexNativeSession(source) && codexPlanModeFromLabels(source?.labels);
}
