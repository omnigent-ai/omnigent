/**
 * Title-case a reasoning-effort level for display: ``"high"`` → ``"High"``,
 * with the conventional camel-cased ``"xhigh"`` → ``"xHigh"``.
 *
 * The single formatter for every surface that renders an effort level — the
 * composer pill, both composers' pickers, and the config modals — so the same
 * level never shows with a different casing depending on the harness that
 * reported it.
 */
export function formatEffortLabel(effort: string): string {
  if (effort.toLowerCase() === "xhigh") return "xHigh";
  return effort.charAt(0).toUpperCase() + effort.slice(1);
}
