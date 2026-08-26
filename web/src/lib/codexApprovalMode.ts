export const CODEX_NATIVE_DEFAULT_APPROVAL_MODE = "default";

// Launch-only Codex full bypass. It rides as a session label because there is
// no live-safe app-server transition into this stance.
export const CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY = "omnigent.codex_native.bypass_sandbox";
export const CODEX_NATIVE_BYPASS_APPROVAL_VALUE = "bypass";

export const CODEX_NATIVE_BYPASS_APPROVAL_OPTION = {
  value: CODEX_NATIVE_BYPASS_APPROVAL_VALUE,
  label: "Bypass approvals & sandbox",
  description: "Runs Codex with no approval prompts and no command sandbox",
  args: [] as string[],
};

export const CODEX_NATIVE_APPROVAL_MODES = [
  {
    value: "default",
    label: "Default",
    description: "Read/edit/run in workspace; approval for external edits or network",
    args: [] as string[],
  },
  {
    value: "full-access",
    label: "Full access",
    description: "Edit any file and access the internet without approval",
    args: ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
  },
  {
    value: "read-only",
    label: "Read only",
    description: "Read files only; approval required for edits, commands, or network",
    args: ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
  },
] as const;

export function codexApprovalModeFromArgs(args: readonly string[] | null | undefined): string {
  if (!args?.length) return CODEX_NATIVE_DEFAULT_APPROVAL_MODE;
  const joined = args.join(" ");
  if (joined.includes("danger-full-access") && joined.includes("never")) return "full-access";
  if (joined.includes("read-only")) return "read-only";
  return CODEX_NATIVE_DEFAULT_APPROVAL_MODE;
}

export function codexApprovalModeFromSession(session: {
  terminalLaunchArgs?: readonly string[] | null;
  labels?: Record<string, string>;
}): string {
  // Full bypass is a launch directive carried as a label rather than a CLI arg.
  // It outranks persisted preset args until the server clears it after a
  // successful live switch, so the active picker never understates the stance.
  if (session.labels?.[CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY] === "1") {
    return CODEX_NATIVE_BYPASS_APPROVAL_VALUE;
  }
  return codexApprovalModeFromArgs(session.terminalLaunchArgs);
}
