// Gating line of an approval card. Harness bridges announce the gated tool as
// "<harness> wants to call|use **<tool>**" (routes_hooks.py, _executor_adapter.py); only that
// exact shape is formatted. Anything else — policy prompts, MCP messages, raw commands — stays verbatim.

const TOOL_NAME_MESSAGE = /^(.+ wants to (?:call|use) )\*\*([^\s*]+)\*\*$/;

export function ElicitationMessage({
  message,
  className,
}: {
  message: string;
  className?: string;
}) {
  const match = TOOL_NAME_MESSAGE.exec(message);
  if (!match) return <span className={className}>{message}</span>;
  return (
    <span className={className}>
      {match[1]}
      <strong className="font-semibold">{match[2]}</strong>
    </span>
  );
}
