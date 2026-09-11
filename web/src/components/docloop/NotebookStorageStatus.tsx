import { useEffect, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";

interface StorageSnapshot {
  revision: string;
  binding_id: string;
  history_status?: { status: string };
}

/** History repair never reloads Lab, replaces drafts, or invokes an execution. */
export function NotebookStorageStatus({
  sessionId,
  active,
}: {
  sessionId: string;
  active: boolean;
}) {
  const [snapshot, setSnapshot] = useState<StorageSnapshot | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const base = `/v1/sessions/${encodeURIComponent(sessionId)}/docloop`;
  useEffect(() => {
    if (!active || busy) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const response = await authenticatedFetch(base + "/document", {
          signal: abort.signal,
          cache: "no-store",
        });
        if (response.ok) {
          const value = await response.json();
          if (!abort.signal.aborted) setSnapshot(value);
        }
      } finally {
        if (!abort.signal.aborted) timer = setTimeout(() => void poll().catch(() => {}), 5000);
      }
    };
    void poll().catch(() => {});
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [active, base, busy]);
  const repair = async () => {
    if (!snapshot || busy) return;
    setBusy(true);
    try {
      const response = await authenticatedFetch(base + "/recover-history", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Docloop-Edit": "1" },
        body: JSON.stringify({ revision: snapshot.revision, binding_id: snapshot.binding_id }),
      });
      const value = await response.json();
      if (!response.ok)
        throw new Error(value.error || "History repair failed. No action was repeated.");
      setSnapshot(value);
      setMessage("Version history verified. No edit, cell execution, or instruction was repeated.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "History repair failed.");
    } finally {
      setBusy(false);
    }
  };
  const status = snapshot?.history_status?.status;
  const needsRepair = status && status !== "recorded";
  if (!snapshot && !message) return null;
  return (
    <div className="docloop-notebook-message" role="status">
      {needsRepair && (
        <p>
          Version history: {status}. The saved document remains available. Repair records its
          current bytes only.
        </p>
      )}
      {message && <p>{message}</p>}
      <button type="button" disabled={busy || !snapshot} onClick={() => void repair()}>
        {needsRepair ? "Repair version history" : "Verify version history"}
      </button>
    </div>
  );
}
