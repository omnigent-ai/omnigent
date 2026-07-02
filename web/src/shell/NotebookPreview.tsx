import type { BundledLanguage } from "shiki";
import AnsiDefault from "ansi-to-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { CodeBlockContent } from "@/components/ai-elements/code-block";
import { cn } from "@/lib/utils";

// ansi-to-react is CJS with a TS-compiled `exports.default`; depending on the
// bundler interop (Vite dev prebundle vs vitest vs production build) the
// default import is either the component or the whole exports object.
const Ansi = ("default" in AnsiDefault ? AnsiDefault.default : AnsiDefault) as typeof AnsiDefault;

// ---------------------------------------------------------------------------
// NotebookPreview — read-only render of a Jupyter notebook (.ipynb, nbformat 4).
//
// Renders cells in order: markdown via react-markdown (same pipeline as
// MarkdownPreview), code via the shared Shiki CodeBlockContent, and outputs
// from each cell's mime bundle. Active content (text/html, javascript) is
// never injected into the DOM — rich outputs fall back to their text/plain
// representation with a note, so hostile notebooks can't run scripts here.
// ---------------------------------------------------------------------------

interface NotebookOutput {
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

function parseNotebook(content: string): { notebook?: Notebook; error?: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
  const nb = parsed as Notebook;
  if (!nb || typeof nb !== "object" || !Array.isArray(nb.cells)) {
    return { error: "not a notebook: missing cells array" };
  }
  return { notebook: nb };
}

function AnsiText({ text, className }: { text: string; className?: string }) {
  return (
    <pre className={cn("whitespace-pre-wrap break-words font-mono text-xs p-2", className)}>
      <Ansi>{text}</Ansi>
    </pre>
  );
}

function OutputView({ output }: { output: NotebookOutput }) {
  if (output.output_type === "stream") {
    return (
      <AnsiText
        text={joinSource(output.text)}
        className={output.name === "stderr" ? "bg-destructive/10" : undefined}
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
  if (imageMime) {
    const b64 = joinSource(data[imageMime]).replace(/\n/g, "");
    return (
      <img
        src={`data:${imageMime};base64,${b64}`}
        alt="notebook output"
        className="max-w-full my-1"
      />
    );
  }

  const plain = data["text/plain"] !== undefined ? joinSource(data["text/plain"]) : undefined;
  const suppressedHtml = data["text/html"] !== undefined;
  if (plain === undefined && !suppressedHtml) return null;
  return (
    <div>
      {suppressedHtml && (
        <div className="text-xs text-muted-foreground italic px-2 pt-1">
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
      <div className="w-14 shrink-0 pt-1 text-right font-mono text-xs text-muted-foreground select-none">
        In [{count ?? " "}]:
      </div>
      <div className="min-w-0 flex-1 space-y-1">
        <div className="rounded border bg-muted/30 text-xs">
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

export function NotebookPreview({ content }: { content: string }) {
  const { notebook, error } = parseNotebook(content);

  if (error || !notebook) {
    return (
      <div className="p-8 text-sm">
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
    <div className="h-full space-y-4 overflow-auto px-6 py-4">
      {(notebook.cells ?? []).map((cell, i) => {
        if (cell.cell_type === "markdown") {
          return (
            // eslint-disable-next-line react/no-array-index-key
            <div key={i} className="prose dark:prose-invert prose-sm max-w-none pl-16">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{joinSource(cell.source)}</ReactMarkdown>
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
