import { Suspense, lazy, useEffect, useState } from "react";
import { officeApi } from "../api.ts";
import { isMarkdownFile } from "./markdownLinks.ts";

const MarkdownReader = lazy(() => import("./MarkdownReader.tsx"));

interface FileViewerProps {
  projectId: string;
  path: string;
  /** Opens another file of the room's folder as a tab, e.g. from a Markdown link. */
  onOpenFile: (path: string) => void;
}

interface Loaded {
  content: string | null;
  truncated: boolean;
  bytes: number | null;
}

type Mode = "reader" | "code";

export function fileSize(bytes: number | null): string {
  if (bytes === null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** A workspace file: Markdown opens in reader mode, everything else in a highlighted code view. */
export function FileViewer({ projectId, path, onOpenFile }: FileViewerProps) {
  const markdown = isMarkdownFile(path);
  const [file, setFile] = useState<Loaded | null>(null);
  const [highlighted, setHighlighted] = useState<{ html: string | null; language: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>(markdown ? "reader" : "code");
  const [wrap, setWrap] = useState(false);
  const [copied, setCopied] = useState(false);
  const showCode = mode === "code";

  useEffect(() => {
    let cancelled = false;
    setFile(null);
    setHighlighted(null);
    setError(null);
    officeApi
      .roomFile(projectId, path)
      .then((loaded) => !cancelled && setFile(loaded))
      .catch((caught: Error) => !cancelled && setError(caught.message));
    return () => {
      cancelled = true;
    };
  }, [projectId, path]);

  // Highlighting (and Shiki itself) loads only when the code view is shown.
  useEffect(() => {
    if (!showCode || highlighted || typeof file?.content !== "string") return;
    let cancelled = false;
    const content = file.content;
    import("./codeHighlight.ts")
      .then(({ highlightFile }) => highlightFile(content, path))
      .catch(() => ({ html: null, language: "text" }))
      .then((result) => !cancelled && setHighlighted(result));
    return () => {
      cancelled = true;
    };
  }, [showCode, highlighted, file, path]);

  const copy = async () => {
    if (!file?.content) return;
    await navigator.clipboard.writeText(file.content).catch(() => undefined);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  const language = file?.content === null ? "binary" : !showCode ? "markdown" : (highlighted?.language ?? "");
  const text = file?.content ?? null;

  return (
    <section className="file-viewer" aria-labelledby="file-viewer-title">
      <header className="file-viewer-header">
        <h2 id="file-viewer-title">{path}</h2>
        <span className="muted small">{file ? [language, fileSize(file.bytes)].filter(Boolean).join(" · ") : "Loading…"}</span>
        <div className="file-viewer-actions">
          {markdown ? (
            <div className="view-mode" role="group" aria-label="View as">
              {(["reader", "code"] as const).map((entry) => (
                <button key={entry} type="button" aria-pressed={mode === entry} onClick={() => setMode(entry)}>
                  {entry === "reader" ? "Reader" : "Code"}
                </button>
              ))}
            </div>
          ) : null}
          {showCode ? (
            <label className="small">
              <input type="checkbox" checked={wrap} onChange={(event) => setWrap(event.target.checked)} /> Wrap lines
            </label>
          ) : null}
          <button type="button" onClick={() => void copy()} disabled={!file?.content}>
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      </header>
      {error ? (
        <p className="error-text" role="alert">
          {error}
        </p>
      ) : null}
      {file?.truncated ? <p className="muted small">Showing the start of the file only.</p> : null}
      {file && text === null ? <p className="muted">This file is not text, so it cannot be shown.</p> : null}
      {text !== null && !showCode ? (
        <Suspense fallback={<p className="muted small">Loading reader…</p>}>
          <MarkdownReader path={path} content={text} onOpenFile={onOpenFile} />
        </Suspense>
      ) : null}
      {text !== null && showCode ? (
        highlighted?.html ? (
          // Shiki escapes the file's text; the markup is only its own spans.
          <div className={`code-view${wrap ? " code-wrap" : ""}`} dangerouslySetInnerHTML={{ __html: highlighted.html }} />
        ) : (
          <pre className={`code-view code-plain${wrap ? " code-wrap" : ""}`}>{text}</pre>
        )
      ) : null}
    </section>
  );
}
