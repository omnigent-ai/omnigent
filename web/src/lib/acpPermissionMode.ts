export const ACP_PERMISSION_MODE_LABEL = "omnigent.acp.permission_mode";

export const ACP_PERMISSION_MODES = [
  {
    value: "auto",
    label: "Ask every time",
    description: "Ask before tools that require approval",
  },
  {
    value: "bypassPermissions",
    label: "Full access",
    description: "Automatically approve tool requests; explicit policies still apply",
  },
];

export function isAcpSession(session: { harness?: string | null } | null | undefined): boolean {
  return session?.harness === "acp" || session?.harness?.startsWith("acp:") === true;
}

export function acpPermissionModeFromSession(
  session: { labels?: Record<string, string | null> | null } | null | undefined,
): string {
  const mode = session?.labels?.[ACP_PERMISSION_MODE_LABEL];
  return ACP_PERMISSION_MODES.some((option) => option.value === mode) ? mode! : "";
}
