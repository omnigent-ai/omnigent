const CODEX_NATIVE_WRAPPER = "codex-native-ui";
// Mirrors the server's label: persisted by the in-session Plan toggle and
// seeded at create by the New Session screen's Plan entry.
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

/** First release whose server and managed runners start a thread from a create-time Plan label. */
export const CODEX_PRELAUNCH_PLAN_MIN_SERVER_VERSION = "0.18.0";

/**
 * Whether a server can honor Plan mode armed before the first prompt. Older
 * servers store the label but their runners ignore it, so the session would
 * run in Default under a Plan badge; an unknown version fails closed.
 */
export function serverSupportsCodexPrelaunchPlanMode(
  serverVersion: string | null | undefined,
): boolean {
  const release = (value: string) => /^(\d+)\.(\d+)\.(\d+)/.exec(value)?.slice(1).map(Number);
  const actual = release(serverVersion ?? "");
  const floor = release(CODEX_PRELAUNCH_PLAN_MIN_SERVER_VERSION)!;
  if (!actual) return false;
  for (let i = 0; i < floor.length; i += 1) {
    if (actual[i] !== floor[i]) return actual[i] > floor[i];
  }
  return true;
}
