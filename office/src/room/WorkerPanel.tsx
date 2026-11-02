import { useEffect, useState } from "react";
import { type SessionDetail, type SessionItem, type WorkerSession, officeApi } from "../api.ts";
import { type ActivityEntry, toActivity } from "../../shared/sessionActivity.ts";
import { WORKER_STATE_LABELS, type WorkerState } from "../workers.ts";

const POLL_MS = 3_000;

const ACTIVITY_LABELS: Record<ActivityEntry["kind"], string> = {
  you: "You",
  agent: "Worker",
  office: "Office",
  tool: "Tool",
  error: "Error",
};

interface WorkerPanelProps {
  worker: WorkerSession;
  state: WorkerState;
  /** The role restarted in a newer session; this one is kept for reading. */
  replaced?: boolean;
  onClose: () => void;
}

export function WorkerPanel({ worker, state, replaced = false, onClose }: WorkerPanelProps) {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [items, setItems] = useState<SessionItem[]>([]);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [nextDetail, nextItems] = await Promise.all([officeApi.session(worker.id), officeApi.items(worker.id)]);
        if (cancelled) return;
        setDetail(nextDetail);
        // Newest first, as the service returns them and like the room's Activity tab.
        setItems(nextItems);
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

  const activity = toActivity(items);
  const nextSteps = (detail?.todos ?? []).filter((todo) => todo.status !== "completed");

  return (
    <section className="worker-panel" aria-label={`Worker ${worker.title ?? worker.agent_name ?? worker.id}`}>
      <header className="panel-header">
        <div>
          <h2>{worker.title ?? "Untitled session"}</h2>
          {worker.labels?.["office.role"] ? <p className="muted small">SDLC role session · office branch {worker.labels["office.branch"]}</p> : null}
          <p className="muted">
            {detail?.harness ?? worker.agent_name ?? "Generalist"}
            {detail?.llm_model ? ` · ${detail.llm_model}` : ""}
          </p>
          <p className={`state-pill state-${state}`}>{WORKER_STATE_LABELS[state]}</p>
          {replaced ? <p className="muted small">Replaced: the role now runs in a newer session, shown in the Team tab.</p> : null}
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
      <h3>
        Activity <span className="muted small">newest first</span>
      </h3>
      <ol className="activity" aria-live="polite">
        {activity.length === 0 ? <li className="muted">No visible activity yet.</li> : null}
        {activity.map((entry) => (
          <li key={entry.id} className={`activity-${entry.kind}`}>
            <span className="activity-kind">{ACTIVITY_LABELS[entry.kind]}</span>
            <span>{entry.text}</span>
          </li>
        ))}
      </ol>
      <p className="muted small">
        Shows messages, tool use, and stated plans only; private reasoning is never displayed. Message this worker below the office.
      </p>
      {notice ? <p role="status">{notice}</p> : null}
    </section>
  );
}
