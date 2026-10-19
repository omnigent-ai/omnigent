import { memo, useRef, type RefObject, type UIEvent } from "react";
import type { BundledLanguage } from "shiki";
import AnsiDefault from "ansi-to-react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { CodeBlockContent } from "@/components/ai-elements/code-block";
import { cn } from "@/lib/utils";
import { ExternalPreviewLink } from "./ExternalPreviewLink";
import { isWorkspaceRelativeSrc } from "./TipTapWorkspaceImage";

// ansi-to-react is CJS with a TS-compiled `exports.default`; depending on the
// bundler interop (Vite dev prebundle vs vitest vs production build) the
// default import is either the component or the whole exports object.
const Ansi = ("default" in AnsiDefault ? AnsiDefault.default : AnsiDefault) as typeof AnsiDefault;

// URLs open outside the app window, as in the Markdown preview; a plain anchor
// would navigate the app itself off its server.
const NOTEBOOK_MARKDOWN_COMPONENTS: Components = {
  a: ({ node: _node, href, ...props }) =>
    href && !isWorkspaceRelativeSrc(href) ? (
      <ExternalPreviewLink href={href} {...props} />
    ) : (
      <a href={href} {...props} />
    ),
};

// ---------------------------------------------------------------------------
// NotebookPreview — read-only render of a Jupyter notebook (.ipynb, nbformat 4).
//
// Renders cells in order: markdown via react-markdown (same pipeline as
// MarkdownPreview), code via the shared Shiki CodeBlockContent, and outputs
// from each cell's mime bundle. Active content (text/html, javascript) is
// never injected into the DOM — rich outputs fall back to their text/plain
// representation with a note, so hostile notebooks can't run scripts here.
// ---------------------------------------------------------------------------

export interface NotebookOutput {
  output_type: string;
  name?: string; // stream: stdout | stderr
  text?: string | string[];
  data?: Record<string, string | string[]>;
  ename?: string;
  evalue?: string;
  traceback?: string[];
}

interface NotebookCell {
  cell_type: string;
  source?: string | string[];
  execution_count?: number | null;
  outputs?: NotebookOutput[];
}

interface Notebook {
  nbformat?: number;
  cells?: NotebookCell[];
  metadata?: { language_info?: { name?: string } };
}

// nbformat stores text as a list of lines (or a single string); normalize.
function joinSource(src: string | string[] | undefined): string {
  if (src === undefined) return "";
  return Array.isArray(src) ? src.join("") : src;
}

// Raster images are inert; SVG and HTML can carry scripts, so they are not
// rendered directly.
const SAFE_IMAGE_MIMES = ["image/png", "image/jpeg", "image/gif", "image/webp"];

// A data-URI built from malformed base64 (wrong length, stray chars, data after
// padding) is rejected by the browser with ERR_INVALID_URL — showing a broken
// image with no explanation. Validate before building the URI so we can fall
// back to a note instead. Length must be a multiple of 4 with padding only at
// the end; length % 4 === 1 is never valid base64.
function isValidBase64(b64: string): boolean {
  return /^[A-Za-z0-9+/]*={0,2}$/.test(b64) && b64.length % 4 === 0;
}

// Notebooks in the wild embed raw C0 control characters (most often ANSI escape
// sequences in traceback/output text) directly inside JSON string literals,
// which strict JSON.parse rejects ("Bad control character in string literal").
// Escape any control char that appears *inside* a string to its \uXXXX form,
// leaving structural whitespace between tokens untouched.
function escapeControlCharsInStrings(content: string): string {
  let out = "";
  let inString = false;
  let escaped = false;
  for (let i = 0; i < content.length; i++) {
    const ch = content[i];
    if (!inString) {
      out += ch;
      if (ch === '"') inString = true;
      continue;
    }
    if (escaped) {
      out += ch;
      escaped = false;
    } else if (ch === "\\") {
      out += ch;
      escaped = true;
    } else if (ch === '"') {
      out += ch;
      inString = false;
    } else if (content.charCodeAt(i) < 0x20) {
      out += `\\u${content.charCodeAt(i).toString(16).padStart(4, "0")}`;
    } else {
      out += ch;
    }
  }
  return out;
}

function parseNotebook(content: string): { notebook?: Notebook; error?: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    // Retry once with stray control characters escaped — real notebooks often
    // contain unescaped ANSI codes in cell output that strict JSON rejects.
    try {
      parsed = JSON.parse(escapeControlCharsInStrings(content));
    } catch (e) {
      return { error: e instanceof Error ? e.message : String(e) };
    }
  }
  const nb = parsed as Notebook;
  if (!nb || typeof nb !== "object" || !Array.isArray(nb.cells)) {
    return { error: "not a notebook: missing cells array" };
  }
  return { notebook: nb };
}

// Long output renders in pieces, each ending at the first newline after
// ANSI_PIECE_CHARS characters. Appending never changes earlier pieces, so a
// streamed batch re-parses only the last one.
const ANSI_PIECE_CHARS = 4096;
const ESC_BRACKET = "\u001b[";

/**
 * The SGR (color/style) state ansi-to-react's parser (anser) holds: the codes
 * that set the foreground and background, and the decoration codes in the
 * order pushed. Duplicates are kept, since a partial reset removes only one.
 */
interface SgrState {
  fg: readonly string[] | null;
  bg: readonly string[] | null;
  decorations: readonly string[];
}

const EMPTY_SGR: SgrState = { fg: null, bg: null, decorations: [] };
const DECORATION_CODES = new Set([1, 2, 3, 4, 5, 7, 8, 9]);
const DECORATION_RESETS: Record<number, readonly string[]> = {
  21: ["1"],
  22: ["1", "2"],
  23: ["3"],
  24: ["4"],
  25: ["5"],
  27: ["7"],
  28: ["8"],
  29: ["9"],
};

/** Apply one SGR parameter list, following anser's `processChunk` rule for rule. */
function applySgr(state: SgrState, params: string): SgrState {
  let { fg, bg } = state;
  let decorations = [...state.decorations];
  const nums = params.split(";");
  while (nums.length > 0) {
    const num = parseInt(nums.shift()!);
    if (isNaN(num) || num === 0) {
      fg = null;
      bg = null;
      decorations = [];
    } else if (DECORATION_CODES.has(num)) {
      decorations.push(String(num));
    } else if (DECORATION_RESETS[num]) {
      for (const code of DECORATION_RESETS[num]) {
        const index = decorations.indexOf(code);
        if (index >= 0) decorations.splice(index, 1);
      }
    } else if (num === 39) {
      fg = null;
    } else if (num === 49) {
      bg = null;
    } else if ((num >= 30 && num < 38) || (num >= 90 && num < 98)) {
      fg = [String(num)];
    } else if ((num >= 40 && num < 48) || (num >= 100 && num < 108)) {
      bg = [String(num)];
    } else if ((num === 38 || num === 48) && nums.length >= 1) {
      const mode = nums.shift();
      let color: string[] | null = null;
      if (mode === "5" && nums.length >= 1) {
        const index = parseInt(nums.shift()!);
        if (index >= 0 && index <= 255) color = [String(num), "5", String(index)];
      } else if (mode === "2" && nums.length >= 3) {
        const rgb = nums.splice(0, 3).map((value) => parseInt(value));
        if (rgb.every((value) => value >= 0 && value <= 255))
          color = [String(num), "2", ...rgb.map(String)];
      }
      if (color && num === 38) fg = color;
      else if (color) bg = color;
    }
  }
  return { fg, bg, decorations };
}

/** One SGR sequence that recreates *state* from a fresh parser. */
function sgrPrefix(state: SgrState): string {
  const codes = [...(state.fg ?? []), ...(state.bg ?? []), ...state.decorations];
  return codes.length > 0 ? `${ESC_BRACKET}${codes.join(";")}m` : "";
}

// ansi-to-react rewrites carriage returns and backspaces before parsing codes;
// both stay within a line. Copies of its `escapeCarriageReturn` and `fixBackspace`.
function escapeCarriageReturn(text: string): string {
  if (!text.includes("\r")) return text;
  let out = text.replace(/\r+\n/gm, "\n");
  while (/\r./.test(out)) {
    out = out.replace(
      /^([^\r\n]*)\r+([^\r\n]+)/gm,
      (_, base: string, insert: string) => insert + base.slice(insert.length),
    );
  }
  return out;
}

function fixBackspace(text: string): string {
  if (!text.includes("\b")) return text;
  let out = text;
  let previous: string;
  do {
    previous = out;
    // Anything but a newline, followed by a backspace (a control character on purpose).
    // oxlint-disable-next-line no-control-regex
    out = previous.replace(/[^\n]\u0008/gm, "");
  } while (out.length < previous.length);
  return out;
}

/** The SGR state after *text*, starting from *before*. Linear in the text. */
function sgrStateAfter(before: SgrState, rawText: string): SgrState {
  const text = escapeCarriageReturn(fixBackspace(rawText));
  let state = before;
  for (let at = text.indexOf(ESC_BRACKET); at !== -1; at = text.indexOf(ESC_BRACKET, at + 2)) {
    let end = at + 2;
    while (end < text.length && "0123456789;".includes(text[end]!)) end++;
    if (text[end] === "m") state = applySgr(state, text.slice(at + 2, end));
  }
  return state;
}

function splitAnsiPieces(text: string): { start: number; text: string }[] {
  const pieces: { start: number; text: string }[] = [];
  let start = 0;
  while (text.length - start > ANSI_PIECE_CHARS) {
    const newline = text.indexOf("\n", start + ANSI_PIECE_CHARS);
    if (newline === -1) break;
    pieces.push({ start, text: text.slice(start, newline + 1) });
    start = newline + 1;
  }
  pieces.push({ start, text: text.slice(start) });
  return pieces;
}

const AnsiPiece = memo(function AnsiPiece({ text }: { text: string }) {
  return <Ansi>{text}</Ansi>;
});

function AnsiText({
  text,
  className,
  nowrap = false,
}: {
  text: string;
  className?: string;
  nowrap?: boolean;
}) {
  // The SGR state at the end of each settled piece, reused while the piece is unchanged.
  const settled = useRef<{ text: string; end: SgrState }[]>([]);
  const pieces = splitAnsiPieces(text);
  let state = EMPTY_SGR;
  const rendered = pieces.map((piece, index) => {
    // Each piece starts with one code recreating the state in effect.
    const prefixed = sgrPrefix(state) + piece.text;
    if (index < pieces.length - 1) {
      const cached = settled.current[index];
      if (cached?.text === piece.text) {
        state = cached.end;
      } else {
        state = sgrStateAfter(state, piece.text);
        settled.current.length = index;
        settled.current.push({ text: piece.text, end: state });
      }
    }
    return <AnsiPiece key={piece.start} text={prefixed} />;
  });
  // Outputs (tracebacks especially) contain long unbroken runs — separator
  // rules, file paths — that word-wrapping can't split. Wrap where possible
  // (overflow-wrap) but let anything unbreakable scroll horizontally within the
  // cell rather than pushing the whole preview wide.
  return (
    <pre
      className={cn(
        "overflow-x-auto font-mono text-sm p-2",
        nowrap ? "whitespace-pre" : "whitespace-pre-wrap [overflow-wrap:anywhere]",
        className,
      )}
    >
      {rendered}
    </pre>
  );
}

/**
 * One notebook output. Also renders chat code run output as `stream` outputs.
 *
 * @param nowrap - Scroll long lines horizontally instead of wrapping them.
 */
export function OutputView({ output, nowrap }: { output: NotebookOutput; nowrap?: boolean }) {
  if (output.output_type === "stream") {
    return (
      <AnsiText
        text={joinSource(output.text)}
        className={output.name === "stderr" ? "bg-destructive/10" : undefined}
        nowrap={nowrap}
      />
    );
  }

  if (output.output_type === "error") {
    return <AnsiText text={(output.traceback ?? []).join("\n")} className="bg-destructive/10" />;
  }

  // execute_result / display_data carry a mime bundle; pick the richest safe
  // representation.
  const data = output.data ?? {};
  const imageMime = SAFE_IMAGE_MIMES.find((m) => data[m] !== undefined);
  let imageError: string | undefined;
  if (imageMime) {
    // Strip *all* whitespace, not just newlines: base64 payloads split across
    // JSON-array lines can carry CRLF or stray spaces, and a data-URI with any
    // whitespace in it is rejected by the browser (renders as a broken image).
    const b64 = joinSource(data[imageMime]).replace(/\s/g, "");
    if (isValidBase64(b64)) {
      return (
        <img
          src={`data:${imageMime};base64,${b64}`}
          alt="notebook output"
          className="max-w-full my-1"
        />
      );
    }
    // Corrupt payload: don't emit a broken <img> — note it and fall through to
    // the text/plain repr below (matplotlib etc. usually include one).
    imageError = `Image output (${imageMime}) could not be decoded.`;
  }

  const plain = data["text/plain"] !== undefined ? joinSource(data["text/plain"]) : undefined;
  const suppressedHtml = data["text/html"] !== undefined;
  if (plain === undefined && !suppressedHtml && imageError === undefined) return null;
  return (
    <div>
      {imageError && (
        <div className="text-sm text-muted-foreground italic px-2 pt-1">{imageError}</div>
      )}
      {suppressedHtml && (
        <div className="text-sm text-muted-foreground italic px-2 pt-1">
          Rich HTML output hidden — showing plain text.
        </div>
      )}
      {plain !== undefined && <AnsiText text={plain} />}
    </div>
  );
}

function CodeCell({ cell, language }: { cell: NotebookCell; language: BundledLanguage }) {
  const count = cell.execution_count;
  return (
    <div className="flex gap-2">
      <div className="w-14 shrink-0 pt-1 text-right font-mono text-sm text-muted-foreground select-none">
        In [{count ?? " "}]:
      </div>
      <div className="min-w-0 flex-1 space-y-1">
        <div className="rounded border bg-muted/30 text-sm">
          <CodeBlockContent code={joinSource(cell.source)} language={language} />
        </div>
        {(cell.outputs ?? []).map((output, i) => (
          // eslint-disable-next-line react/no-array-index-key
          <OutputView key={i} output={output} />
        ))}
      </div>
    </div>
  );
}

export function NotebookPreview({
  content,
  rootRef,
  onScroll,
}: {
  content: string;
  rootRef?: RefObject<HTMLDivElement | null>;
  onScroll?: (event: UIEvent<HTMLElement>) => void;
}) {
  const { notebook, error } = parseNotebook(content);

  if (error || !notebook) {
    return (
      <div className="p-8 text-ui">
        <div className="text-destructive">Cannot render notebook: {error}</div>
        <div className="mt-1 text-muted-foreground">
          Switch to the source view to inspect the raw file.
        </div>
      </div>
    );
  }

  const langName = notebook.metadata?.language_info?.name ?? "python";
  const language = langName as BundledLanguage;

  return (
    <div
      ref={rootRef}
      data-preview-scroll
      onScroll={onScroll}
      className="h-full space-y-4 overflow-auto px-6 py-4"
    >
      {(notebook.cells ?? []).map((cell, i) => {
        if (cell.cell_type === "markdown") {
          return (
            // eslint-disable-next-line react/no-array-index-key
            <div key={i} className="prose dark:prose-invert prose-sm max-w-none pl-16">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={NOTEBOOK_MARKDOWN_COMPONENTS}>
                {joinSource(cell.source)}
              </ReactMarkdown>
            </div>
          );
        }
        if (cell.cell_type === "code") {
          // eslint-disable-next-line react/no-array-index-key
          return <CodeCell key={i} cell={cell} language={language} />;
        }
        // raw (and any unknown cell type): show the source verbatim.
        // eslint-disable-next-line react/no-array-index-key
        return <AnsiText key={i} text={joinSource(cell.source)} className="pl-16 opacity-70" />;
      })}
    </div>
  );
}
