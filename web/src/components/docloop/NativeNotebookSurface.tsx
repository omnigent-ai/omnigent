import { useEffect, useRef, useState } from "react";
import { authenticatedFetch, fetchWithBrowserSession } from "@/lib/identity";
import { mountNotebookPane } from "./notebook-pane.mjs";
import { NotebookHistory } from "./NotebookHistory";

/** Keep Lab mounted while switching to Chat so its kernel connection survives. */
export function NativeNotebookSurface({
  sessionId,
  active,
}: {
  sessionId: string;
  active: boolean;
}) {
  const [started, setStarted] = useState(false);
  const [url, setUrl] = useState<string | null>(null);
  const [message, setMessage] = useState("Opening notebook…");
  const [source, setSource] = useState(false);
  const [history, setHistory] = useState(false);
  const [org, setOrg] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [kernelStatus, setKernelStatus] = useState("Loading JupyterLab…");
  const editor = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  useEffect(() => {
    if (!url || !frame.current) return;
    const iframe = frame.current;
    let stopped = false;
    let restored = false;
    let compactBefore: boolean | undefined;
    interface Lab {
      restored: Promise<void>;
      shell: {
        mode: string;
        currentWidget?: { sessionContext?: { kernelDisplayStatus?: string } };
        collapseLeft: () => void;
        collapseRight: () => void;
      };
    }
    let lab: Lab | undefined;
    const resize = () => {
      if (!restored || !lab || iframe.clientWidth === 0) return;
      const compact = iframe.clientWidth < 900;
      if (compact === compactBefore) return;
      compactBefore = compact;
      lab.shell.mode = compact ? "single-document" : "multiple-document";
      if (compact) {
        lab.shell.collapseLeft();
        lab.shell.collapseRight();
      }
    };
    const timer = window.setInterval(() => {
      let current: Lab | undefined;
      try {
        current = (iframe.contentWindow as (Window & { jupyterapp?: Lab }) | null)?.jupyterapp;
      } catch {
        clearInterval(timer);
        setKernelStatus("Open Notebook again to return to JupyterLab.");
        return;
      }
      if (restored && current) {
        const status = current.shell.currentWidget?.sessionContext?.kernelDisplayStatus;
        setKernelStatus(status ? `Kernel: ${status}` : "JupyterLab ready");
      }
      if (!current || current === lab) return;
      lab = current;
      void current.restored.then(() => {
        if (!stopped && lab === current) {
          restored = true;
          resize();
        }
      });
    }, 250);
    const observer = new ResizeObserver(resize);
    observer.observe(iframe);
    return () => {
      stopped = true;
      clearInterval(timer);
      observer.disconnect();
    };
  }, [url]);
  useEffect(() => {
    if (active) setStarted(true);
  }, [active]);
  useEffect(() => {
    if (!started) return;
    const abort = new AbortController();
    const base = `/v1/sessions/${encodeURIComponent(sessionId)}/docloop`;
    void (async () => {
      try {
        const response = await authenticatedFetch(base + "/document", { signal: abort.signal });
        if (!response.ok)
          throw new Error("Send a message in Chat to open this session’s notebook.");
        const document = await response.json();
        if (document.format === "org") {
          setOrg(true);
          setSource(true);
          return;
        }
        // An iframe uses the browser's session, independently of JS-only host headers.
        const native = await fetchWithBrowserSession(base + "/jupyter", abort.signal);
        if (!native.ok)
          throw new Error(
            "Jupyter needs a signed-in browser session. You can still open Source and use Chat.",
          );
        const descriptor = await native.json();
        if (
          typeof descriptor.url !== "string" ||
          !descriptor.url.startsWith(base + "/jupyter/lab/tree/")
        ) {
          throw new Error(descriptor.error || "Jupyter is unavailable for this environment.");
        }
        setUrl(descriptor.url);
        setMessage("");
      } catch (error) {
        if (!abort.signal.aborted)
          setMessage(error instanceof Error ? error.message : "Notebook unavailable.");
      }
    })();
    return () => abort.abort();
  }, [started, sessionId, attempt]);
  useEffect(() => {
    if (!source || !editor.current) return;
    const pane = mountNotebookPane(editor.current, { sessionId, fetcher: authenticatedFetch });
    return () => pane.dispose();
  }, [source, sessionId]);
  if (!started) return null;
  return (
    <div className="docloop-notebook-surface">
      <div className="docloop-notebook-toolbar" aria-label="Notebook editor">
        {!org && (
          <button
            type="button"
            aria-pressed={!source && !history}
            onClick={() => {
              setSource(false);
              setHistory(false);
            }}
          >
            JupyterLab
          </button>
        )}
        <button
          type="button"
          aria-pressed={source && !history}
          onClick={() => {
            setSource(true);
            setHistory(false);
          }}
        >
          {org ? "Document" : "Source"}
        </button>
        <button type="button" aria-pressed={history} onClick={() => setHistory(true)}>
          History
        </button>
        {url && (
          <a href={url} target="_blank" rel="noreferrer">
            Open full page ↗
          </a>
        )}
      </div>
      {!source && !history && message && (
        <div className="docloop-notebook-message">
          <p role="status">{message}</p>
          <button type="button" onClick={() => setAttempt((value) => value + 1)}>
            Try again
          </button>
        </div>
      )}
      {url && !source && !history && (
        <p className="docloop-kernel-status" role="status">
          {kernelStatus}
        </p>
      )}
      {url && (
        // oxlint-disable-next-line react/iframe-missing-sandbox -- Lab is a first-party authenticated app; compact layout uses same-origin access.
        <iframe
          ref={frame}
          src={url}
          title="JupyterLab notebook"
          hidden={source || history}
          className="docloop-jupyter-frame"
        />
      )}
      <div ref={editor} hidden={!source || history} className="docloop-source-editor" />
      {history && <NotebookHistory key={sessionId} sessionId={sessionId} />}
    </div>
  );
}
