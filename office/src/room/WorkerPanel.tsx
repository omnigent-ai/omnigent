import { useEffect, useState } from "react";
import { type SessionDetail, type SessionItem, type WorkerSession, officeApi } from "../api.ts";
import { WORKER_STATE_LABELS, deriveWorkerState, toActivity } from "../workers.ts";

const POLL_MS = 3_000;

interface WorkerPanelProps {
  worker: WorkerSession;
  onClose: () => void;
}

export function WorkerPanel({ worker, onClose }: WorkerPanelProps) {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [items, setItems] = useState<SessionItem[]>([]);
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [nextDetail, nextItems] = await Promise.all([officeApi.session(worker.id), officeApi.items(worker.id)]);
        if (cancelled) return;
        setDetail(nextDetail);
        // The service returns newest first; show chronologically.
        setItems([...nextItems].reverse());
      } catch (error) {
        if (!cancelled) setNotice(`Could not load activity: ${(error as Error).message}`);
      }
    };
    void load();
    const timer = window.setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [worker.id]);

  const state = deriveWorkerState(worker);
  const activity = toActivity(items);
  const nextSteps = (detail?.todos ?? []).filter((todo) => todo.status !== "completed");

  const send = async () => {
    const text = draft.trim();
    if (!text) return;
    setSending(true);
    setNotice(null);
    try {
      const result = await officeApi.sendMessage(worker.id, text);
      if (result.denied) setNotice("A policy blocked this message.");
      else setDraft("");
    } catch (error) {
      setNotice(`Not sent: ${(error as Error).message}`);
    } finally {
      setSending(false);
    }
  };

  const stop = async () => {
    try {
      await officeApi.stop(worker.id);
      setNotice("Stop sent.");
    } catch (error) {
      setNotice(`Stop failed: ${(error as Error).message}`);
    }
  };

  return (
    <section className="worker-panel" aria-label={`Worker ${worker.title ?? worker.agent_name ?? worker.id}`}>
      <header className="panel-header">
        <div>
          <h2>{worker.title ?? "Untitled session"}</h2>
          <p className="muted">
            {detail?.harness ?? worker.agent_name ?? "Generalist"}
            {detail?.llm_model ? ` · ${detail.llm_model}` : ""}
          </p>
          <p className={`state-pill state-${state}`}>{WORKER_STATE_LABELS[state]}</p>
        </div>
        <button type="button" onClick={onClose} aria-label="Close worker panel">
          ✕
        </button>
      </header>
      {detail?.last_task_error?.message && state === "failed" ? (
        <p className="error-text">Last error: {detail.last_task_error.message}</p>
      ) : null}
      {nextSteps.length ? (
        <div>
          <h3>Stated next steps</h3>
          <ul className="todo-list">
            {nextSteps.map((todo) => (
              <li key={todo.content} className={todo.status === "in_progress" ? "todo-active" : undefined}>
                {todo.status === "in_progress" ? (todo.activeForm ?? todo.content) : todo.content}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <h3>Activity</h3>
      <ol className="activity" aria-live="polite">
        {activity.length === 0 ? <li className="muted">No visible activity yet.</li> : null}
        {activity.map((entry) => (
          <li key={entry.id} className={`activity-${entry.kind}`}>
            <span className="activity-kind">{entry.kind === "you" ? "You" : entry.kind === "agent" ? "Worker" : entry.kind === "tool" ? "Tool" : "Error"}</span>
            <span>{entry.text}</span>
          </li>
        ))}
      </ol>
      <p className="muted small">Shows messages, tool use, and stated plans only; private reasoning is never displayed.</p>
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <label htmlFor={`message-${worker.id}`}>Message this worker</label>
        <textarea
          id={`message-${worker.id}`}
          value={draft}
          rows={3}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <div className="composer-actions">
          <button type="submit" disabled={sending || !draft.trim()}>
            Send
          </button>
          <button type="button" className="danger" onClick={() => void stop()} disabled={state !== "working" && state !== "waiting_human"}>
            Stop
          </button>
        </div>
      </form>
      {notice ? <p role="status">{notice}</p> : null}
    </section>
  );
}
