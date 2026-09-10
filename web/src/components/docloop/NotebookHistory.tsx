import { useEffect, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";
import { prettyJSON } from "./display-json.mjs";

interface Version {
  commit: string;
  date: string;
  message: string;
}
interface Page {
  binding_id: string;
  versions: Version[];
  next_before: string | null;
}
interface Snapshot {
  document_name: string;
  nodes: {
    id: string;
    kind: string;
    language: string;
    source: string;
    source_truncated: boolean;
    output_text: string;
    output_truncated: boolean;
  }[];
}

/** Read saved copies without replacing the live editor or its kernel. */
export function NotebookHistory({ sessionId }: { sessionId: string }) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState<Page | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const before = cursors[cursors.length - 1];
  const base = `/v1/sessions/${encodeURIComponent(sessionId)}/docloop/versions`;
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError("");
    setPage(null);
    setSelected(null);
    void (async () => {
      try {
        const response = await authenticatedFetch(base + (before ? `?before=${before}` : ""), {
          signal: abort.signal,
          cache: "no-store",
        });
        if (!response.ok) throw new Error("Saved versions are unavailable. Try refreshing.");
        const value = await response.json();
        if (
          value.schema_version !== 1 ||
          value.session_id !== sessionId ||
          !Array.isArray(value.versions)
        )
          throw new Error("History does not match this session.");
        if (!abort.signal.aborted) setPage(value);
      } catch (cause) {
        if (!abort.signal.aborted)
          setError(cause instanceof Error ? cause.message : "History unavailable.");
      } finally {
        if (!abort.signal.aborted) setLoading(false);
      }
    })();
    return () => abort.abort();
  }, [base, before, refresh, sessionId]);
  useEffect(() => {
    setSnapshot(null);
    if (!selected || !page) return;
    const abort = new AbortController();
    setError("");
    void (async () => {
      try {
        const response = await authenticatedFetch(`${base}/${selected}`, {
          signal: abort.signal,
          cache: "no-store",
        });
        if (!response.ok) throw new Error("This saved copy is unavailable. Refresh history.");
        const value = await response.json();
        if (
          value.session_id !== sessionId ||
          value.commit !== selected ||
          value.binding_id !== page.binding_id ||
          value.read_only !== true
        )
          throw new Error("Notebook binding changed. Refresh history.");
        if (!abort.signal.aborted) setSnapshot(value.document);
      } catch (cause) {
        if (!abort.signal.aborted)
          setError(cause instanceof Error ? cause.message : "Saved copy unavailable.");
      }
    })();
    return () => abort.abort();
  }, [base, page, selected, sessionId]);
  return (
    <section className="docloop-history" aria-label="Notebook history">
      <header>
        <div>
          <h2>Saved versions</h2>
          <p>Open a saved copy. Your live notebook stays open.</p>
        </div>
        <button
          type="button"
          disabled={loading}
          onClick={() => {
            setCursors([null]);
            setRefresh((value) => value + 1);
          }}
        >
          Refresh history
        </button>
      </header>
      {error && <p role="alert">{error}</p>}
      <div className="docloop-history-body">
        <nav aria-label="Saved notebook versions">
          {loading && <p role="status">Loading saved versions…</p>}
          {page?.versions.length === 0 && <p>No saved versions yet.</p>}
          {page?.versions.map((version) => (
            <button
              type="button"
              key={version.commit}
              aria-pressed={selected === version.commit}
              onClick={() => setSelected(version.commit)}
            >
              <strong>{version.message || "Saved notebook"}</strong>
              <span>
                {new Date(version.date).toLocaleString()} · {version.commit.slice(0, 8)}
              </span>
            </button>
          ))}
          <div className="docloop-history-pages">
            <button
              type="button"
              disabled={loading || cursors.length === 1}
              onClick={() => setCursors((value) => value.slice(0, -1))}
            >
              Newer
            </button>
            <button
              type="button"
              disabled={loading || !page?.next_before}
              onClick={() => {
                if (page?.next_before) setCursors((value) => [...value, page.next_before]);
              }}
            >
              Older
            </button>
          </div>
        </nav>
        <div className="docloop-history-copy" aria-live="polite">
          {!selected && <p>Choose a version to view its cells and saved output.</p>}
          {selected && !snapshot && !error && <p role="status">Opening saved copy…</p>}
          {snapshot && (
            <>
              <h3>{snapshot.document_name}</h3>
              <p>Saved copy · read only · {selected?.slice(0, 12)}</p>
              {snapshot.nodes.map((node) => (
                <article key={node.id}>
                  <h4>
                    {node.kind} · {node.id}
                  </h4>
                  <pre>
                    {["json", "mcp", "team"].includes(node.language)
                      ? prettyJSON(node.source)
                      : node.source}
                  </pre>
                  {node.source_truncated && <p>Source preview truncated.</p>}
                  {node.output_text && (
                    <pre aria-label={`Saved output ${node.id}`}>{prettyJSON(node.output_text)}</pre>
                  )}
                  {node.output_truncated && <p>Output preview truncated.</p>}
                </article>
              ))}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
