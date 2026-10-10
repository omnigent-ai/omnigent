// The gating line of an approval card, e.g. "Claude wants to call **Bash**".
//
// Harness bridges wrap the gated tool in `**…**` (routes_hooks.py,
// _executor_adapter.py) and nothing else in an elicitation message is markup:
// policy prompts and MCP servers put raw commands and paths in the same field,
// where a full markdown pass would turn `pkg/__init__.py` into a bold "init" or
// `*.o *.a` into italics. So only `**bold**` runs are interpreted; everything
// else stays literal.

import { Fragment } from "react";

export interface MessageRun {
  text: string;
  bold: boolean;
}

// `**` hugging non-whitespace on both sides, with no `*` inside — the
// CommonMark strong-emphasis shape the bridges emit. `****`, `** x**` and a
// lone `**` never match and render verbatim.
const BOLD_RUN = /\*\*(\S(?:[^*]*?\S)?)\*\*/g;

/** Split `message` into literal and bold runs, in order. */
export function splitMessageRuns(message: string): MessageRun[] {
  const runs: MessageRun[] = [];
  let last = 0;
  for (const match of message.matchAll(BOLD_RUN)) {
    if (match.index > last) runs.push({ text: message.slice(last, match.index), bold: false });
    runs.push({ text: match[1], bold: true });
    last = match.index + match[0].length;
  }
  if (last < message.length) runs.push({ text: message.slice(last), bold: false });
  return runs;
}

export function ElicitationMessage({
  message,
  className,
}: {
  message: string;
  className?: string;
}) {
  // Keyed by position in the rendered text; runs are never empty, so positions are unique.
  let offset = 0;
  return (
    <span className={className}>
      {splitMessageRuns(message).map((run) => {
        const key = offset;
        offset += run.text.length;
        return run.bold ? (
          <strong key={key} className="font-semibold">
            {run.text}
          </strong>
        ) : (
          <Fragment key={key}>{run.text}</Fragment>
        );
      })}
    </span>
  );
}
