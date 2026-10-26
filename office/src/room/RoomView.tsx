import { useCallback, useEffect, useMemo, useState } from "react";
import { PRIORITY_LABELS, explainRank } from "../../shared/ranking.ts";
import { GATE_TITLES, isAgentRole, roleTitle } from "../../shared/sdlcTheme.ts";
import type { Priority, RoomSettingsPatch, RoomSummary } from "../../shared/types.ts";
import type { BranchView } from "../../shared/workflowTypes.ts";
import { type WorkerSession, officeApi } from "../api.ts";
import { WORKER_STATE_LABELS, deriveWorkerState } from "../workers.ts";
import { ApprovalDialog, type ApprovalRequest, decideApproval } from "./ApprovalDialog.tsx";
import { RequestAlert } from "./RequestAlert.tsx";
import { RoomCanvas } from "./RoomCanvas.tsx";
import { MAX_DESKS, buildRoomLayout } from "./roomLayout.ts";
import { WorkerPanel } from "./WorkerPanel.tsx";
import { openProblems, problemAnchor } from "./workflow/AgentProblems.tsx";
import { ThemeSetup } from "./workflow/ThemeSetup.tsx";
import { WorkflowPanel } from "./workflow/WorkflowPanel.tsx";
import { type WorkflowRequest, WorkflowRequestDialog, decideWorkflowRequest } from "./workflow/WorkflowRequestDialog.tsx";

const POLL_MS = 3_000;

interface RoomViewProps {
  room: RoomSummary;
  allRooms: RoomSummary[];
  serverNow: number;
  placementWaiting: boolean;
  onExit: () => void;
  onUpdateRoom: (patch: RoomSettingsPatch) => Promise<void>;
}

export function workerName(worker: WorkerSession): string {
  return worker.title ?? worker.agent_name ?? "Untitled session";
}

/** Role identity from the session's durable office labels. */
function workerRole(worker: WorkerSession): string | null {
  const role = worker.labels?.["office.role"];
  if (!isAgentRole(role)) return null;
  return `${roleTitle(role)}${worker.labels?.["office.temporary"] === "true" ? " (temporary)" : ""}`;
}

function workflowRequests(view: BranchView | null): WorkflowRequest[] {
  if (!view) return [];
  return [
    ...view.gates
      .filter((gate) => gate.status === "pending")
      .map((gate) => ({ kind: "gate" as const, gate, task: view.tasks.find((task) => task.id === gate.taskId) ?? null })),
    ...view.proposals
      .filter((proposal) => proposal.status === "pending")
      .map((proposal) => ({ kind: "proposal" as const, proposal, board: view.board })),
  ];
}

export function RoomView({ room, allRooms, serverNow, placementWaiting, onExit, onUpdateRoom }: RoomViewProps) {
  const [workers, setWorkers] = useState<WorkerSession[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [requests, setRequests] = useState<ApprovalRequest[]>([]);
  const [selectedWorkerId, setSelectedWorkerId] = useState<string | null>(null);
  const [openRequest, setOpenRequest] = useState<ApprovalRequest | null>(null);
  const [showRank, setShowRank] = useState(false);
  const [branchView, setBranchView] = useState<BranchView | null>(null);
  const [branchLoaded, setBranchLoaded] = useState(false);
  const [openWorkflowRequest, setOpenWorkflowRequest] = useState<WorkflowRequest | null>(null);

  const load = useCallback(async () => {
    try {
      setBranchView(await officeApi.branch(room.projectId));
      setBranchLoaded(true);
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
      if (event.key === "Escape" && !openRequest && !openWorkflowRequest) {
        if (selectedWorkerId) setSelectedWorkerId(null);
        else onExit();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openRequest, openWorkflowRequest, selectedWorkerId, onExit]);

  const layout = useMemo(() => buildRoomLayout(workers.length), [workers.length]);
  const selectedWorker = workers.find((worker) => worker.id === selectedWorkerId) ?? null;
  const explanation = explainRank(room, allRooms, serverNow, placementWaiting);
  const pendingWorkflow = workflowRequests(branchView);
  const openQuestion = branchView?.interviews.find((question) => question.status === "asked") ?? null;
  const humanTasks = (branchView?.tasks ?? []).filter((task) => task.status === "blocked" && task.responsible.includes("po"));
  const problems = branchView ? openProblems(branchView) : [];
  const nothingPending =
    requests.length === 0 && pendingWorkflow.length === 0 && !openQuestion && humanTasks.length === 0 && problems.length === 0;

  return (
    <div className="room-view">
      <div className="room-main">
        <header className="room-header">
          <div>
            <h1>
              {room.icon ? `${room.icon} ` : ""}
              {room.name}
            </h1>
            <p className="muted">
              {branchView
                ? "SDLC theme · you are the Product Owner · the PM coordinates"
                : "Unthemed project · each session works as a generalist"}
            </p>
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
          {nothingPending ? <p className="muted">No requests need your attention.</p> : null}
          <ul>
            {problems.map((entry) => (
              <li key={`problem:${entry.sessionId ?? entry.role}`}>
                <button
                  type="button"
                  className="request-button request-problem"
                  onClick={() => {
                    if (!branchView) return;
                    const card = document.getElementById(problemAnchor(branchView.branch.id, entry));
                    card?.scrollIntoView({ block: "center" });
                    card?.focus();
                  }}
                >
                  ⚠ {roleTitle(entry.role)}
                  {entry.temporary ? " (temporary)" : ""} is stuck: {entry.problem?.title}
                </button>
              </li>
            ))}
            {pendingWorkflow.map((request) => (
              <li key={request.kind === "gate" ? request.gate.id : request.proposal.id}>
                <RequestAlert
                  label={
                    request.kind === "gate"
                      ? `${GATE_TITLES[request.gate.gate]} gate (${roleTitle(request.gate.requestedBy)}): ${request.gate.summary.slice(0, 80)}`
                      : `Board proposal from the PM (${request.proposal.requirements.length} requirements)`
                  }
                  detail={
                    request.kind === "gate" && request.gate.operation ? (
                      <>
                        Allows <code>{request.gate.operation}</code>
                      </>
                    ) : undefined
                  }
                  approveLabel={request.kind === "gate" ? "Approve" : "Accept"}
                  onApprove={() => decideWorkflowRequest(branchView?.branch.id ?? "", request, true)}
                  onDetails={() => setOpenWorkflowRequest(request)}
                  onChanged={() => void load()}
                />
              </li>
            ))}
            {openQuestion ? (
              <li>
                <button
                  type="button"
                  className="request-button"
                  onClick={() => document.getElementById("interview-answer")?.focus()}
                >
                  ⚑ PM interview question: {openQuestion.question.slice(0, 90)}
                </button>
              </li>
            ) : null}
            {humanTasks.map((task) => (
              <li key={task.id} className="muted">
                ⚑ Task waiting on you: {task.title}
              </li>
            ))}
            {requests.map((request) => (
              <li key={`${request.sessionId}:${request.elicitation.elicitation_id}`}>
                <RequestAlert
                  label={`${request.workerName}: ${(request.elicitation.params?.message ?? "Approval requested").slice(0, 90)}`}
                  approveLabel="Approve"
                  onApprove={() => decideApproval(request, "accept")}
                  onDetails={() => setOpenRequest(request)}
                  onChanged={() => void load()}
                />
              </li>
            ))}
          </ul>
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
          inputEnabled={!openRequest && !openWorkflowRequest}
        />
        {branchView ? (
          <WorkflowPanel view={branchView} onChanged={() => void load()} />
        ) : branchLoaded ? (
          <ThemeSetup room={room} onAssigned={() => void load()} />
        ) : null}
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
                        {workerRole(worker) ?? worker.agent_name ?? "Generalist"} · {WORKER_STATE_LABELS[state]}
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
      {openWorkflowRequest && branchView ? (
        <WorkflowRequestDialog
          branchId={branchView.branch.id}
          request={openWorkflowRequest}
          onClose={() => setOpenWorkflowRequest(null)}
          onDecided={() => {
            setOpenWorkflowRequest(null);
            void load();
          }}
        />
      ) : null}
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
