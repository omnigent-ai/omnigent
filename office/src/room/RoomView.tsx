import { useCallback, useEffect, useMemo, useState } from "react";
import { PRIORITY_LABELS, explainRank } from "../../shared/ranking.ts";
import type { Priority, RoomSettingsPatch, RoomSummary } from "../../shared/types.ts";
import { type WorkerSession, officeApi } from "../api.ts";
import { WORKER_STATE_LABELS, deriveWorkerState } from "../workers.ts";
import { ApprovalDialog, type ApprovalRequest } from "./ApprovalDialog.tsx";
import { RoomCanvas } from "./RoomCanvas.tsx";
import { MAX_DESKS, buildRoomLayout } from "./roomLayout.ts";
import { WorkerPanel } from "./WorkerPanel.tsx";

const POLL_MS = 3_000;

interface RoomViewProps {
  room: RoomSummary;
  allRooms: RoomSummary[];
  serverNow: number;
  placementWaiting: boolean;
  onExit: () => void;
  onUpdateRoom: (patch: RoomSettingsPatch) => Promise<void>;
}

function workerName(worker: WorkerSession): string {
  return worker.title ?? worker.agent_name ?? "Untitled session";
}

export function RoomView({ room, allRooms, serverNow, placementWaiting, onExit, onUpdateRoom }: RoomViewProps) {
  const [workers, setWorkers] = useState<WorkerSession[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [requests, setRequests] = useState<ApprovalRequest[]>([]);
  const [selectedWorkerId, setSelectedWorkerId] = useState<string | null>(null);
  const [openRequest, setOpenRequest] = useState<ApprovalRequest | null>(null);
  const [showRank, setShowRank] = useState(false);

  const load = useCallback(async () => {
    try {
      // Oldest first, so desks keep their seats as new workers arrive.
      const nextWorkers = [...(await officeApi.workers(room.projectId))].sort((a, b) => a.created_at - b.created_at);
      setWorkers(nextWorkers);
      setLoadError(null);
      const waiting = nextWorkers.filter((worker) => worker.pending_elicitations_count > 0);
      const details = await Promise.all(waiting.map((worker) => officeApi.session(worker.id).catch(() => null)));
      setRequests(
        details.flatMap((detail, index) =>
          (detail?.pending_elicitations ?? []).map((elicitation) => ({
            sessionId: waiting[index].id,
            workerName: workerName(waiting[index]),
            elicitation,
          })),
        ),
      );
    } catch (error) {
      setLoadError((error as Error).message);
    }
  }, [room.projectId]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !openRequest) {
        if (selectedWorkerId) setSelectedWorkerId(null);
        else onExit();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openRequest, selectedWorkerId, onExit]);

  const layout = useMemo(() => buildRoomLayout(workers.length), [workers.length]);
  const selectedWorker = workers.find((worker) => worker.id === selectedWorkerId) ?? null;
  const explanation = explainRank(room, allRooms, serverNow, placementWaiting);

  return (
    <div className="room-view">
      <div className="room-main">
        <header className="room-header">
          <div>
            <h1>
              {room.icon ? `${room.icon} ` : ""}
              {room.name}
            </h1>
            <p className="muted">Unthemed project · each session works as a generalist</p>
          </div>
          <div className="room-controls">
            <label>
              Priority{" "}
              <select
                value={room.priority}
                onChange={(event) => void onUpdateRoom({ priority: event.target.value as Priority })}
              >
                {(Object.keys(PRIORITY_LABELS) as Priority[]).map((priority) => (
                  <option key={priority} value={priority}>
                    {PRIORITY_LABELS[priority]}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <input
                type="checkbox"
                checked={room.keepUpstairs}
                onChange={(event) => void onUpdateRoom({ keepUpstairs: event.target.checked })}
              />{" "}
              Keep upstairs
            </label>
            <button type="button" aria-expanded={showRank} onClick={() => setShowRank((shown) => !shown)}>
              Explain rank
            </button>
            <button type="button" onClick={onExit}>
              ⬅ Back to elevator
            </button>
          </div>
        </header>
        {showRank ? (
          <div className="rank-explanation" role="note">
            <strong>{explanation.headline}.</strong> {explanation.details.join(" ")}
          </div>
        ) : null}
        <section aria-label="Requests needing your decision" className="requests">
          {requests.length === 0 ? (
            <p className="muted">No requests need your attention.</p>
          ) : (
            <ul>
              {requests.map((request) => (
                <li key={`${request.sessionId}:${request.elicitation.elicitation_id}`}>
                  <button type="button" className="request-button" onClick={() => setOpenRequest(request)}>
                    ⚑ {request.workerName}: {(request.elicitation.params?.message ?? "Approval requested").slice(0, 90)}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
        {loadError ? (
          <p className="error-text" role="alert">
            Cannot load workers: {loadError}
          </p>
        ) : null}
        <RoomCanvas
          layout={layout}
          workers={workers}
          selectedWorkerId={selectedWorkerId}
          onOpenWorker={setSelectedWorkerId}
          onExit={onExit}
          inputEnabled={!openRequest}
        />
      </div>
      <aside className="side-panel">
        {selectedWorker ? (
          <WorkerPanel key={selectedWorker.id} worker={selectedWorker} onClose={() => setSelectedWorkerId(null)} />
        ) : (
          <section aria-label="Workers">
            <h2>Workers</h2>
            {workers.length === 0 ? <p className="muted">No sessions in this project yet.</p> : null}
            <ul className="worker-list">
              {workers.map((worker, index) => {
                const state = deriveWorkerState(worker);
                return (
                  <li key={worker.id}>
                    <button type="button" onClick={() => setSelectedWorkerId(worker.id)}>
                      <span className={`state-dot state-${state}`} aria-hidden="true" />
                      <span className="worker-name">{workerName(worker)}</span>
                      <span className="muted small">
                        {worker.agent_name ?? "agent"} · {WORKER_STATE_LABELS[state]}
                        {index >= MAX_DESKS ? " · no desk (room full)" : ""}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        )}
      </aside>
      {openRequest ? (
        <ApprovalDialog
          request={openRequest}
          onClose={() => setOpenRequest(null)}
          onDecided={() => {
            setOpenRequest(null);
            void load();
          }}
        />
      ) : null}
    </div>
  );
}
