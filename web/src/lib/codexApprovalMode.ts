export const CODEX_NATIVE_DEFAULT_APPROVAL_MODE = "default";

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
