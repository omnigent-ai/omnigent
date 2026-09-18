import { useEffect, useMemo, useState, type ReactNode } from "react";
import { mermaid } from "@streamdown/mermaid";
import type { MermaidErrorComponentProps, MermaidOptions } from "streamdown";

// Mermaid's parsers open every syntax error with this line.
const ERROR_LINE_RE = /^(?:Parse|Lexical) error on line (\d+)/;

// Mermaid strips these before parsing (see preprocessDiagram in mermaid 11), so
// its line numbers count from the stripped text. Regexes copied from mermaid;
// the tests parse through mermaid itself, so drift shows up there. The last
// pattern replays the trailing `trimStart()`.
const FRONT_MATTER_RE = /^-{3}\s*[\n\r](.*?)[\n\r]-{3}\s*[\n\r]+/s;
const STRIPPED_BEFORE_PARSING = [
  new RegExp(FRONT_MATTER_RE.source, "gs"),
  /%{2}{\s*(?:(\w+)\s*:|(\w+))\s*(?:(\w+)|((?:(?!}%{2}).|\r?\n)*))?\s*(?:}%{2})?/gi,
  /^\s*%%(?!{)[^\n]+\n?/gm,
  /^\s+/g,
];

// In a sequence diagram `;` ends the statement, so a semicolon meant as
// punctuation inside free text — a block label, a participant alias, or the
// message and note text after the colon — cuts the statement short. These pick
// out exactly those free-text spans.
const BLOCK_LABEL_RE =
  /^(\s*(?:alt|else|opt|loop|par|par_over|and|critical|option|break|rect|box|title)\b\s*)(.*)$/i;
const ALIAS_RE = /^(\s*(?:participant|actor)\s+\S+\s+as\s+)(.*)$/i;
const ARROW = String.raw`<<-{1,2}>>|-{1,2}(?:>{1,2}|x|\)|\|[\\/]|\\\\|\/\/)|(?:\/\/|\\\\|\/\||\\\|)-{1,2}`;
const TEXT_AFTER_COLON_RE = new RegExp(
  String.raw`^(\s*(?:note\b[^:]*|title\s*|[^:]*?(?:${ARROW})[^:]*)):(.*)$`,
  "i",
);

// A `;` closing a Mermaid entity code such as `#59;` or `#9829;` is already an
// escape; only a bare semicolon needs one.
const ENTITY_OR_SEMICOLON_RE = /#\w+;|;/g;

function hasBareSemicolon(text: string): boolean {
  return text.replace(/#\w+;/g, "").includes(";");
}

// `;` ends a statement in sequence diagrams, and prose in a Note or message
// routinely carries one — the classic LLM slip.
const SEMICOLON_HINT = (
  <>
    Mermaid reads <code>;</code> as the end of a statement in sequence diagrams. Write{" "}
    <code>#59;</code> for a literal semicolon.
  </>
);

export interface EscapedSemicolons {
  text: string;
  count: number;
  /** 1-based line in the author's source holding the first escaped semicolon. */
  firstLine: number;
}

/**
 * Escape every bare `;` inside the free text of a sequence diagram as `#59;`,
 * the form Mermaid documents for a literal semicolon. Existing entity codes and
 * the YAML front matter are left alone. Null when nothing changed. Only for a
 * diagram that already failed to parse: in a valid one a `;` may be a
 * deliberate statement separator.
 */
export function escapeSequenceTextSemicolons(chart: string): EscapedSemicolons | null {
  const normalized = chart.replace(/\r\n?/g, "\n");
  const frontMatter = FRONT_MATTER_RE.exec(normalized)?.[0] ?? "";
  const frontMatterLines = frontMatter.split("\n").length - 1;
  let count = 0;
  let firstLine = 0;
  const escape = (text: string, lineIndex: number) =>
    text.replace(ENTITY_OR_SEMICOLON_RE, (match) => {
      if (match !== ";") return match;
      count += 1;
      if (firstLine === 0) firstLine = frontMatterLines + lineIndex + 1;
      return "#59;";
    });
  const lines = normalized
    .slice(frontMatter.length)
    .split("\n")
    .map((line, index) => {
      if (!hasBareSemicolon(line)) return line;
      const block = BLOCK_LABEL_RE.exec(line);
      if (block) return block[1] + escape(block[2], index);
      const alias = ALIAS_RE.exec(line);
      if (alias) return alias[1] + escape(alias[2], index);
      const text = TEXT_AFTER_COLON_RE.exec(line);
      if (text) return `${text[1]}:${escape(text[2], index)}`;
      return line;
    });
  return count === 0 ? null : { text: frontMatter + lines.join("\n"), count, firstLine };
}

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
  /** The diagram with its text semicolons escaped, when that may be all that is wrong. */
  escaped: EscapedSemicolons | null;
}

export function describeMermaidError(chart: string, error: string): MermaidErrorDetails {
  const reported = ERROR_LINE_RE.exec(error);
  const located = reported ? locateParsedLine(chart, Number(reported[1]) - 1) : null;
  if (!located) return { line: null, source: null, hint: null, escaped: null };
  const sequence = /^sequenceDiagram\b/i.test(located.parsed);
  return {
    line: located.line,
    source: located.source,
    hint: sequence && hasBareSemicolon(located.source) ? SEMICOLON_HINT : null,
    escaped: sequence ? escapeSequenceTextSemicolons(chart) : null,
  };
}

type EscapedRender =
  | { status: "skipped" }
  | { status: "rendering" }
  | { status: "rendered"; svg: string; count: number; firstLine: number }
  | { status: "failed" };

// Render the escaped diagram through the same mermaid instance Streamdown
// uses. Only the error path pays for this: a diagram that parsed first time
// never reaches this component.
function useEscapedRender(escaped: EscapedSemicolons | null): EscapedRender {
  const [state, setState] = useState<EscapedRender>({ status: escaped ? "rendering" : "skipped" });
  useEffect(() => {
    if (!escaped) {
      setState({ status: "skipped" });
      return;
    }
    let cancelled = false;
    setState({ status: "rendering" });
    const id = `mermaid-escaped-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
    mermaid
      .getMermaid()
      .render(id, escaped.text)
      .then(({ svg }) => {
        if (cancelled) return;
        setState({ status: "rendered", svg, count: escaped.count, firstLine: escaped.firstLine });
      })
      .catch(() => {
        if (!cancelled) setState({ status: "failed" });
      });
    return () => {
      cancelled = true;
    };
  }, [escaped]);
  return state;
}

function RawDetails({ chart, error }: { chart: string; error: string }) {
  return (
    <details className="mt-2 text-muted-foreground text-xs">
      <summary className="cursor-pointer">Details</summary>
      <pre className="mt-1 whitespace-pre-wrap font-mono">{error}</pre>
      <pre className="mt-2 whitespace-pre-wrap font-mono">{chart}</pre>
    </details>
  );
}

/**
 * Replaces Streamdown's default mermaid error block, which dumps the parser's
 * raw message: a caret line plus every token the grammar could have accepted.
 *
 * A sequence diagram whose only fault is punctuation semicolons is rendered
 * with them escaped, under a note saying so. Anything else shows what a reader
 * can act on — the failing line and, for known pitfalls, the fix — with the raw
 * message and source folded away for those who want them.
 */
export function MermaidError({ chart, error }: MermaidErrorComponentProps) {
  const details = useMemo(() => describeMermaidError(chart, error), [chart, error]);
  const escaped = useEscapedRender(details.escaped);
  if (escaped.status === "rendering") {
    return (
      <div className="my-4 flex justify-center p-4">
        <div className="flex items-center space-x-2 text-muted-foreground">
          <div className="h-4 w-4 animate-spin rounded-full border-current border-b-2" />
          <span className="text-sm">Loading diagram...</span>
        </div>
      </div>
    );
  }
  if (escaped.status === "rendered") {
    return (
      <div data-testid="mermaid-escaped">
        <div
          aria-label="Mermaid chart"
          className="flex justify-center [&_svg]:h-auto [&_svg]:max-w-full"
          // Mermaid's own render under securityLevel "strict": the same trusted,
          // sanitized markup Streamdown injects for a diagram that parsed first time.
          dangerouslySetInnerHTML={{ __html: escaped.svg }}
          role="img"
        />
        <p className="mt-2 text-muted-foreground text-xs">
          Rendered with {escaped.count === 1 ? "one semicolon" : `${escaped.count} semicolons`}{" "}
          escaped as <code>#59;</code> (first on line {escaped.firstLine}). Mermaid reads{" "}
          <code>;</code> as the end of a statement in sequence diagrams, so the source will not
          render as written elsewhere.
        </p>
        <RawDetails chart={chart} error={error} />
      </div>
    );
  }
  return (
    <div
      data-testid="mermaid-error"
      className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm"
    >
      <p className="font-medium text-destructive">
        {details.line === null
          ? "Mermaid couldn't render this diagram"
          : `Mermaid couldn't parse line ${details.line}`}
      </p>
      {details.source === null ? (
        <p className="mt-1 text-muted-foreground">{error.split("\n")[0]}</p>
      ) : (
        <pre className="mt-2 overflow-x-auto rounded bg-muted px-2 py-1 font-mono text-xs">
          <code>{details.source.trim()}</code>
        </pre>
      )}
      {details.hint !== null && <p className="mt-2 text-muted-foreground">{details.hint}</p>}
      <RawDetails chart={chart} error={error} />
    </div>
  );
}

export const MERMAID_STREAMDOWN_OPTIONS: MermaidOptions = { errorComponent: MermaidError };
