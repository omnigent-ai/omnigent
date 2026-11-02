import { useState } from "react";
import { roleTitle } from "../../shared/sdlcTheme.ts";
import { coordinatorName } from "../../shared/team.ts";
import type { WorkerSession } from "../api.ts";
import { WORKER_STATE_LABELS, type WorkerState } from "../workers.ts";
import { CollapseButton } from "./PaneRail.tsx";
import { MAX_DESKS } from "./roomLayout.ts";
import { WorkerPanel } from "./WorkerPanel.tsx";

export function workerName(worker: WorkerSession): string {
  return worker.title ?? worker.agent_name ?? "Untitled session";
}

/** Role identity from the session's durable office labels. */
function workerRole(worker: WorkerSession): string | null {
  const role = worker.labels?.["office.role"];
  if (!role) return null;
  return `${roleTitle(role)}${worker.labels?.["office.temporary"] === "true" ? " (temporary)" : ""}`;
}

/** A live temporary worker of the room, with the task it was started for. */
export interface TemporaryWorker {
  task: string | null;
  /** Its task is done or cancelled, so closing it loses nothing. */
  finished: boolean;
}

interface WorkerRowProps {
  worker: WorkerSession;
  state: WorkerState;
  note: string | null;
  onOpen: (sessionId: string) => void;
  temporary?: TemporaryWorker;
  /** What closing means, asked before closing; the row offers × only with it. */
  closeConfirm?: string;
  closeTitle?: string;
  onClose?: () => Promise<void>;
}

function WorkerRow({ worker, state, note, onOpen, temporary, closeConfirm, closeTitle, onClose }: WorkerRowProps) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  return (
    <li className={closeConfirm && onClose ? "worker-row-closable" : undefined}>
      <button type="button" onClick={() => onOpen(worker.id)}>
        <span className={`state-dot state-${state}`} aria-hidden="true" />
        <span className="worker-name">{workerName(worker)}</span>
        <span className="muted small">
          {workerRole(worker) ?? worker.agent_name ?? "Generalist"} · {WORKER_STATE_LABELS[state]}
          {temporary?.finished ? " · task finished" : ""}
          {note ? ` · ${note}` : ""}
        </span>
      </button>
      {closeConfirm && onClose && !confirming ? (
        <button type="button" className="worker-close" aria-label={`Close ${workerName(worker)}`} title={closeTitle} onClick={() => setConfirming(true)}>
          ×
        </button>
      ) : null}
      {closeConfirm && onClose && confirming ? (
        <div className="worker-close-confirm" role="group" aria-label={`Close ${workerName(worker)}?`}>
          <span className="small">{closeConfirm}</span>
          <button
            type="button"
            className="danger"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              void onClose().finally(() => {
                setBusy(false);
                setConfirming(false);
              });
            }}
          >
            {busy ? "Closing…" : "Close"}
          </button>
          <button type="button" disabled={busy} onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </div>
      ) : null}
    </li>
  );
}

interface OfficeBoardProps {
  current: WorkerSession[];
  replaced: WorkerSession[];
  stateOf: (worker: WorkerSession) => WorkerState;
  selected: WorkerSession | null;
  onSelect: (sessionId: string | null) => void;
  onCollapse: () => void;
  /** Live temporary workers by session id; only these can be closed. */
  temporaries: Map<string, TemporaryWorker>;
  /** Close temporary workers; resolves with an error message, if any. */
  onCloseTemporary: (sessionIds: string[]) => Promise<string | null>;
  /** Close replaced sessions; resolves with an error message, if any. */
  onCloseReplaced: (sessionIds: string[]) => Promise<string | null>;
}

const REPLACED_CLOSE_CONFIRM = "Close this replaced session? It stops and is archived in Omnigent; the role's current session is not affected.";

function temporaryCloseConfirm(temporary: TemporaryWorker | undefined): string | undefined {
  if (!temporary) return undefined;
  return temporary.finished ? "Close this finished temporary worker?" : `Its task is not finished. Close anyway? The ${coordinatorName()} can dispatch the task again.`;
}

/** The room's Omnigent sessions; choosing one opens it here to read and message. */
export function OfficeBoard({ current, replaced, stateOf, selected, onSelect, onCollapse, temporaries, onCloseTemporary, onCloseReplaced }: OfficeBoardProps) {
  const replacedIds = new Set(replaced.map((worker) => worker.id));
  const [error, setError] = useState<string | null>(null);
  const [clearing, setClearing] = useState<"ask" | "busy" | null>(null);
  const [clearingReplaced, setClearingReplaced] = useState<"ask" | "busy" | null>(null);
  const finished = current.filter((worker) => temporaries.get(worker.id)?.finished).map((worker) => worker.id);
  const close = async (sessionIds: string[]) => setError(await onCloseTemporary(sessionIds));
  const closeReplaced = async (sessionIds: string[]) => setError(await onCloseReplaced(sessionIds));
  return (
    <aside className="room-pane office-board" aria-label="Office Board">
      <div className="pane-header">
        <h2>
          Office Board <span className="muted small">{current.length} sessions</span>
        </h2>
        <CollapseButton side="left" label="Office Board" onCollapse={onCollapse} />
      </div>
      {!selected && finished.length > 0 ? (
        <div className="clear-finished">
          {clearing === null ? (
            <button type="button" className="link-button" onClick={() => setClearing("ask")}>
              Clear {finished.length} finished temporary worker{finished.length === 1 ? "" : "s"}
            </button>
          ) : (
            <>
              <span className="small">Close {finished.length} whose tasks are done?</span>
              <button
                type="button"
                className="danger"
                disabled={clearing === "busy"}
                onClick={() => {
                  setClearing("busy");
                  void close(finished).finally(() => setClearing(null));
                }}
              >
                {clearing === "busy" ? "Closing…" : "Close"}
              </button>
              <button type="button" disabled={clearing === "busy"} onClick={() => setClearing(null)}>
                Cancel
              </button>
            </>
          )}
        </div>
      ) : null}
      {error ? (
        <p className="error-text small" role="alert">
          {error}
        </p>
      ) : null}
      {selected ? (
        <WorkerPanel
          key={selected.id}
          worker={selected}
          state={stateOf(selected)}
          replaced={replacedIds.has(selected.id)}
          onClose={() => onSelect(null)}
        />
      ) : (
        <>
          {current.length === 0 ? <p className="muted">No sessions in this project yet.</p> : null}
          <ul className="worker-list">
            {current.map((worker, index) => (
              <WorkerRow
                key={worker.id}
                worker={worker}
                state={stateOf(worker)}
                note={index >= MAX_DESKS ? "no desk (room full)" : null}
                onOpen={onSelect}
                temporary={temporaries.get(worker.id)}
                closeConfirm={temporaryCloseConfirm(temporaries.get(worker.id))}
                closeTitle="Close this temporary worker"
                onClose={() => close([worker.id])}
              />
            ))}
          </ul>
          {replaced.length ? (
            <details className="replaced-workers">
              <summary>Replaced sessions ({replaced.length})</summary>
              <p className="muted small">Earlier sessions of roles that restarted with a handoff. They keep their own status until you close them.</p>
              <div className="clear-finished">
                {clearingReplaced === null ? (
                  <button type="button" className="link-button" onClick={() => setClearingReplaced("ask")}>
                    Close all {replaced.length} replaced session{replaced.length === 1 ? "" : "s"}
                  </button>
                ) : (
                  <>
                    <span className="small">Stop and archive {replaced.length} in Omnigent?</span>
                    <button
                      type="button"
                      className="danger"
                      disabled={clearingReplaced === "busy"}
                      onClick={() => {
                        setClearingReplaced("busy");
                        void closeReplaced(replaced.map((worker) => worker.id)).finally(() => setClearingReplaced(null));
                      }}
                    >
                      {clearingReplaced === "busy" ? "Closing…" : "Close all"}
                    </button>
                    <button type="button" disabled={clearingReplaced === "busy"} onClick={() => setClearingReplaced(null)}>
                      Cancel
                    </button>
                  </>
                )}
              </div>
              <ul className="worker-list">
                {replaced.map((worker) => (
                  <WorkerRow
                    key={worker.id}
                    worker={worker}
                    state={stateOf(worker)}
                    note="replaced"
                    onOpen={onSelect}
                    closeConfirm={REPLACED_CLOSE_CONFIRM}
                    closeTitle="Close and archive this replaced session"
                    onClose={() => closeReplaced([worker.id])}
                  />
                ))}
              </ul>
            </details>
          ) : null}
        </>
      )}
    </aside>
  );
}
