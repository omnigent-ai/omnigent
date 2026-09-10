import { useEffect, useRef, useState } from "react";
import { authenticatedFetch, fetchWithBrowserSession } from "@/lib/identity";
import { mountNotebookPane } from "./notebook-pane.mjs";
import { NotebookHistory } from "./NotebookHistory";

const LOAD_TIMEOUT_MS = 120_000;

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
  const [failure, setFailure] = useState<string | null>(null);
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
    const fail = (reason: string) => {
      if (stopped) return;
      stopped = true;
      clearInterval(timer);
      clearTimeout(deadline);
      setFailure(reason);
    };
    const deadline = window.setTimeout(
      () =>
        fail("JupyterLab did not finish loading within two minutes. Retry or use Source and Chat."),
      LOAD_TIMEOUT_MS,
    );
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
        fail("JupyterLab is no longer accessible. Retry or open it in a full page.");
        return;
      }
      if (restored && current) {
        const status = current.shell.currentWidget?.sessionContext?.kernelDisplayStatus;
        setKernelStatus(status ? `Kernel: ${status}` : "JupyterLab ready");
      }
      if (!current || current === lab) return;
      lab = current;
      void current.restored
        .then(() => {
          if (!stopped && lab === current) {
            restored = true;
            clearTimeout(deadline);
            resize();
          }
        })
        .catch(() => {
          if (lab === current)
            fail("JupyterLab could not restore the notebook. Retry or use Source and Chat.");
        });
    }, 250);
    const observer = new ResizeObserver(resize);
    observer.observe(iframe);
    return () => {
      stopped = true;
      clearInterval(timer);
      clearTimeout(deadline);
      observer.disconnect();
    };
  }, [url]);
  useEffect(() => {
    if (active) setStarted(true);
  }, [active]);
  useEffect(() => {
    if (!started) return;
    const abort = new AbortController();
    const deadline = window.setTimeout(() => {
      setFailure("Opening the notebook timed out. Retry or use Source and Chat.");
      abort.abort();
    }, LOAD_TIMEOUT_MS);
    const base = `/v1/sessions/${encodeURIComponent(sessionId)}/docloop`;
    void (async () => {
      try {
        const response = await authenticatedFetch(base + "/document", { signal: abort.signal });
        if (!response.ok)
          throw new Error("Send a message in Chat to open this session’s notebook.");
        const document = await response.json();
        if (abort.signal.aborted) return;
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
        if (abort.signal.aborted) return;
        setUrl(descriptor.url);
        setMessage("");
      } catch (error) {
        if (!abort.signal.aborted)
          setFailure(error instanceof Error ? error.message : "Notebook unavailable.");
      } finally {
        clearTimeout(deadline);
      }
    })();
    return () => {
      abort.abort();
      clearTimeout(deadline);
    };
  }, [started, sessionId, attempt]);
  useEffect(() => {
    if (!source || !editor.current) return;
    const pane = mountNotebookPane(editor.current, { sessionId, fetcher: authenticatedFetch });
    return () => pane.dispose();
  }, [source, sessionId]);
  const retry = () => {
    setUrl(null);
    setFailure(null);
    setMessage("Opening notebook…");
    setKernelStatus("Loading JupyterLab…");
    setAttempt((value) => value + 1);
  };
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
      {!source && !history && (failure || message) && (
        <div className="docloop-notebook-message">
          <p role={failure ? "alert" : "status"}>{failure || message}</p>
          {failure && (
            <button type="button" onClick={retry}>
              Try again
            </button>
          )}
        </div>
      )}
      {url && !failure && !source && !history && (
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
