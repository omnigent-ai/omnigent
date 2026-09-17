import type { ReactNode } from "react";
import type { MermaidErrorComponentProps, MermaidOptions } from "streamdown";

// Mermaid's parsers open every syntax error with this line.
const ERROR_LINE_RE = /^(?:Parse|Lexical) error on line (\d+)/;

// Mermaid strips these before parsing (see preprocessDiagram in mermaid 11), so
// its line numbers count from the stripped text. Regexes copied from mermaid;
// the tests parse through mermaid itself, so drift shows up there. The last
// pattern replays the trailing `trimStart()`.
const STRIPPED_BEFORE_PARSING = [
  /^-{3}\s*[\n\r](.*?)[\n\r]-{3}\s*[\n\r]+/gs,
  /%{2}{\s*(?:(\w+)\s*:|(\w+))\s*(?:(\w+)|((?:(?!}%{2}).|\r?\n)*))?\s*(?:}%{2})?/gi,
  /^\s*%%(?!{)[^\n]+\n?/gm,
  /^\s+/g,
];

// `;` ends a statement in sequence diagrams, and prose in a Note or message
// routinely carries one — the classic LLM slip.
const SEMICOLON_HINT = (
  <>
    Mermaid reads <code>;</code> as the end of a statement in sequence diagrams. Write{" "}
    <code>#59;</code> for a literal semicolon.
  </>
);

interface LocatedLine {
  /** 1-based line in the author's source. */
  line: number;
  /** That line of the author's source. */
  source: string;
  /** The text Mermaid actually parsed. */
  parsed: string;
}

// Replay Mermaid's preprocessing while tracking each surviving character's
// index in the author's text, then read the author's line off the parsed
// line's first character. Matching line contents instead would break on text
// that repeats itself, such as front matter quoting the diagram.
function locateParsedLine(chart: string, parsedLineIndex: number): LocatedLine | null {
  const original = chart.replace(/\r\n?/g, "\n");
  let text = original;
  let indices = Array.from(original, (_char, index) => index);
  for (const pattern of STRIPPED_BEFORE_PARSING) {
    const kept: number[] = [];
    let cursor = 0;
    for (const match of text.matchAll(pattern)) {
      const matchStart = match.index ?? 0;
      for (let i = cursor; i < matchStart; i += 1) kept.push(indices[i]);
      cursor = matchStart + match[0].length;
    }
    for (let i = cursor; i < text.length; i += 1) kept.push(indices[i]);
    indices = kept;
    text = kept.map((index) => original[index]).join("");
  }
  let start = 0;
  for (let n = 0; n < parsedLineIndex; n += 1) {
    const newline = text.indexOf("\n", start);
    if (newline === -1) return null;
    start = newline + 1;
  }
  // A trailing empty line owns no character; it follows the last kept one.
  const originalIndex =
    start < indices.length ? indices[start] : (indices[indices.length - 1] ?? -1) + 1;
  const line = original.slice(0, originalIndex).split("\n").length;
  return { line, source: original.split("\n")[line - 1] ?? "", parsed: text };
}

export interface MermaidErrorDetails {
  /** 1-based line in the author's source, when the error names one. */
  line: number | null;
  /** That line of the author's source. */
  source: string | null;
  hint: ReactNode;
}

export function describeMermaidError(chart: string, error: string): MermaidErrorDetails {
  const reported = ERROR_LINE_RE.exec(error);
  const located = reported ? locateParsedLine(chart, Number(reported[1]) - 1) : null;
  if (!located) return { line: null, source: null, hint: null };
  const sequence = /^sequenceDiagram\b/i.test(located.parsed);
  return {
    line: located.line,
    source: located.source,
    hint: sequence && located.source.includes(";") ? SEMICOLON_HINT : null,
  };
}

/**
 * Replaces Streamdown's default mermaid error block, which dumps the parser's
 * raw message: a caret line plus every token the grammar could have accepted.
 * Show what a reader can act on — the failing line and, for known pitfalls,
 * the fix — and fold the raw message and source away for those who want them.
 */
export function MermaidError({ chart, error }: MermaidErrorComponentProps) {
  const { line, source, hint } = describeMermaidError(chart, error);
  return (
    <div
      data-testid="mermaid-error"
      className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm"
    >
      <p className="font-medium text-destructive">
        {line === null
          ? "Mermaid couldn't render this diagram"
          : `Mermaid couldn't parse line ${line}`}
      </p>
      {source === null ? (
        <p className="mt-1 text-muted-foreground">{error.split("\n")[0]}</p>
      ) : (
        <pre className="mt-2 overflow-x-auto rounded bg-muted px-2 py-1 font-mono text-xs">
          <code>{source.trim()}</code>
        </pre>
      )}
      {hint !== null && <p className="mt-2 text-muted-foreground">{hint}</p>}
      <details className="mt-2 text-muted-foreground text-xs">
        <summary className="cursor-pointer">Details</summary>
        <pre className="mt-1 whitespace-pre-wrap font-mono">{error}</pre>
        <pre className="mt-2 whitespace-pre-wrap font-mono">{chart}</pre>
      </details>
    </div>
  );
}

export const MERMAID_STREAMDOWN_OPTIONS: MermaidOptions = { errorComponent: MermaidError };
