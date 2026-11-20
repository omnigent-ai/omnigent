// Claude Code sub-agents end by calling `SubagentHandback` with their final
// report instead of replying in text, so the report is the child's answer.
export const SUBAGENT_HANDBACK_TOOL = "SubagentHandback";

/**
 * The report a Claude sub-agent handed back to its caller.
 *
 * @param name - The tool call's name.
 * @param args - The tool call's parsed arguments.
 * @returns The report text, or ``null`` when this isn't a handback call
 *   or it carries no report.
 */
export function subagentHandbackReport(name: string, args: Record<string, unknown>): string | null {
  if (name !== SUBAGENT_HANDBACK_TOOL) return null;
  const report = args.message;
  return typeof report === "string" && report.trim() !== "" ? report : null;
}
