import type { ReactNode } from "react";
import type { MermaidErrorComponentProps, MermaidOptions } from "streamdown";

// Mermaid's parsers open every syntax error with this line.
const ERROR_LINE_RE = /^(?:Parse|Lexical) error on line (\d+)/;

// Mermaid strips these before parsing (see preprocessDiagram in mermaid 11), so
// its line numbers count from the stripped text. Regexes copied from mermaid;
// the tests parse through mermaid itself, so drift shows up there.
const FRONT_MATTER_RE = /^-{3}\s*[\n\r](.*?)[\n\r]-{3}\s*[\n\r]+/s;
const DIRECTIVE_RE =
  /%{2}{\s*(?:(\w+)\s*:|(\w+))\s*(?:(\w+)|((?:(?!}%{2}).|\r?\n)*))?\s*(?:}%{2})?/gi;
const COMMENT_RE = /^\s*%%(?!{)[^\n]+\n?/gm;

// `;` ends a statement in sequence diagrams, and prose in a Note or message
// routinely carries one — the classic LLM slip.
const SEMICOLON_HINT = (
  <>
    Mermaid reads <code>;</code> as the end of a statement in sequence diagrams. Write{" "}
    <code>#59;</code> for a literal semicolon.
  </>
);

function cleanupText(code: string): string {
  return code.replace(/\r\n?/g, "\n");
}

function textSeenByParser(chart: string): string {
  return cleanupText(chart)
    .replace(FRONT_MATTER_RE, "")
    .replace(DIRECTIVE_RE, "")
    .replace(COMMENT_RE, "")
    .trimStart();
}

// Only whole lines (and in-line directives) were stripped, so walking the
// cleaned original past the stripped lines recovers the author's line number.
function authorLine(chart: string, parsedLines: string[], index: number): number {
  const original = cleanupText(chart)
    .split("\n")
    .map((line) => line.replace(DIRECTIVE_RE, ""));
  let next = 0;
  for (let i = 0; i < original.length; i += 1) {
    const candidate = next === 0 ? original[i].trimStart() : original[i];
    if (candidate !== parsedLines[next]) continue;
    if (next === index) return i + 1;
    next += 1;
  }
  return index + 1;
}

export interface MermaidErrorDetails {
  /** 1-based line in the author's source, when the error names one. */
  line: number | null;
  /** That line as the parser saw it. */
  source: string | null;
  hint: ReactNode;
}

export function describeMermaidError(chart: string, error: string): MermaidErrorDetails {
  const reported = ERROR_LINE_RE.exec(error);
  if (!reported) return { line: null, source: null, hint: null };
  const parsed = textSeenByParser(chart);
  const parsedLines = parsed.split("\n");
  const index = Number(reported[1]) - 1;
  const source = parsedLines[index] ?? null;
  if (source === null) return { line: null, source: null, hint: null };
  return {
    line: authorLine(chart, parsedLines, index),
    source,
    hint: /^sequenceDiagram\b/i.test(parsed) && source.includes(";") ? SEMICOLON_HINT : null,
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
