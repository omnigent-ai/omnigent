import { useCallback, useEffect, useMemo, useState } from "react";
import { explainRank } from "../../shared/ranking.ts";
import { GATE_TITLES, roleTitle } from "../../shared/sdlcTheme.ts";
import type { RoomSettingsPatch, RoomSummary } from "../../shared/types.ts";
import { MAX_ROOM_MESSAGES, type RoomMessage } from "../../shared/roomMessages.ts";
import type { BranchView } from "../../shared/workflowTypes.ts";
import { type WorkerSession, officeApi } from "../api.ts";
import { roomWorkerState } from "../workers.ts";
import { ApprovalDialog, type ApprovalRequest, decideApproval } from "./ApprovalDialog.tsx";
import { FilesPane } from "./FilesPane.tsx";
import { FileViewer } from "./FileViewer.tsx";
import { registerTeam, teamOf, coordinatorName } from "../../shared/team.ts";
import { roleTargets, sessionTargets } from "./mentions.ts";
import { MessageComposer } from "./MessageComposer.tsx";
import { OfficeBoard, type TemporaryWorker, workerName } from "./OfficeBoard.tsx";
import { CollapseButton, PaneRail } from "./PaneRail.tsx";
import { ProjectBar, type RoomStatus } from "./ProjectBar.tsx";
import { ProjectLinks } from "./ProjectLinks.tsx";
import { RequestAlert } from "./RequestAlert.tsx";
import { RoomCanvas } from "./RoomCanvas.tsx";
import { SlackView } from "./SlackView.tsx";
import { buildRoomLayout } from "./roomLayout.ts";
import { TeamPane } from "./TeamPane.tsx";
import { AgentProblems, openProblems, problemAnchor } from "./workflow/AgentProblems.tsx";
import { BoardModal, type BoardTab } from "./workflow/BoardModal.tsx";
import { TeamConfigModal } from "./workflow/TeamConfigModal.tsx";
import { ThemeSetup } from "./workflow/ThemeSetup.tsx";
import { type WorkflowRequest, WorkflowRequestDialog, decideWorkflowRequest } from "./workflow/WorkflowRequestDialog.tsx";

const POLL_MS = 3_000;
const PANE_STORAGE_KEY = "office.roomPane";

type RightPane = "team" | "files";

const ANIMATION_STORAGE_KEY = "office.animation";
const COLLAPSED_STORAGE_KEY = "office.collapsedPanes";

type Side = "left" | "right";

function savedCollapsed(): Set<Side> {
  try {
    const saved = window.localStorage.getItem(COLLAPSED_STORAGE_KEY) ?? "";
    return new Set(saved.split(",").filter((side): side is Side => side === "left" || side === "right"));
  } catch {
    return new Set();
  }
}

function savedAnimation(): boolean {
  try {
    return window.localStorage.getItem(ANIMATION_STORAGE_KEY) !== "off";
  } catch {
    return true;
  }
}

function savedPane(): RightPane {
  try {
    if (window.localStorage.getItem(PANE_STORAGE_KEY) === "files") return "files";
  } catch {
    // Storage can be unavailable; the Team pane still works.
  }
  return "team";
}

/** Bring a control into view once React has rendered it. */
function focusAfterRender(elementId: string): void {
  window.requestAnimationFrame(() => {
    const element = document.getElementById(elementId);
    element?.scrollIntoView({ block: "center" });
    element?.focus();
  });
}

interface RoomViewProps {
  room: RoomSummary;
  allRooms: RoomSummary[];
  serverNow: number;
  placementWaiting: boolean;
  onExit: () => void;
  onUpdateRoom: (patch: RoomSettingsPatch) => Promise<void>;
}

/**
 * Merge fetched room messages into the shown ones. A fetch that started before a send
 * returns without the new message, so merging (never replacing) keeps it on screen.
 */
export function mergeRoomMessages(current: RoomMessage[], incoming: RoomMessage[]): RoomMessage[] {
  const byId = new Map(current.map((message) => [message.id, message]));
  for (const message of incoming) byId.set(message.id, message);
  return [...byId.values()].sort((a, b) => a.at - b.at).slice(-MAX_ROOM_MESSAGES);
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
  const [branchView, setBranchView] = useState<BranchView | null>(null);
  const [branchLoaded, setBranchLoaded] = useState(false);
  const [openWorkflowRequest, setOpenWorkflowRequest] = useState<WorkflowRequest | null>(null);
  const [rightPane, setRightPane] = useState<RightPane>(savedPane);
  const [roomMessages, setRoomMessages] = useState<RoomMessage[]>([]);
  const [collapsed, setCollapsed] = useState<Set<Side>>(savedCollapsed);

  const setPaneOpen = useCallback((side: Side, open: boolean) => {
    setCollapsed((current) => {
      if (current.has(side) === !open) return current;
      const next = new Set(current);
      if (open) next.delete(side);
      else next.add(side);
      try {
        window.localStorage.setItem(COLLAPSED_STORAGE_KEY, [...next].join(","));
      } catch {
        // Remembering the layout is a convenience only.
      }
      return next;
    });
  }, []);
  const [boardTab, setBoardTab] = useState<BoardTab | null>(null);
  const [teamConfigOpen, setTeamConfigOpen] = useState(false);
  // The middle stage: the office, or one of the files opened from the Files pane.
  const [openFiles, setOpenFiles] = useState<string[]>([]);
  const [stageTab, setStageTab] = useState<string | null>(null);
  const showingOffice = stageTab === null;
  // Off swaps the pixel-art office for a Slack-style chat of the same room.
  const [animation, setAnimation] = useState(savedAnimation);
  const toggleAnimation = () => {
    const next = !animation;
    setAnimation(next);
    try {
      window.localStorage.setItem(ANIMATION_STORAGE_KEY, next ? "on" : "off");
    } catch {
      // Remembering the choice is a convenience only.
    }
  };

  const openFile = useCallback((path: string) => {
    setOpenFiles((current) => (current.includes(path) ? current : [...current, path]));
    setStageTab(path);
  }, []);

  const closeFile = (path: string) => {
    const index = openFiles.indexOf(path);
    const remaining = openFiles.filter((entry) => entry !== path);
    setOpenFiles(remaining);
    // Closing the shown file shows its neighbour, or the office when none is left.
    if (stageTab === path) setStageTab(remaining[Math.min(index, remaining.length - 1)] ?? null);
  };

  const showPane = useCallback((pane: RightPane) => {
    setRightPane(pane);
    try {
      window.localStorage.setItem(PANE_STORAGE_KEY, pane);
    } catch {
      // Remembering the pane is a convenience only.
    }
  }, []);

  const load = useCallback(async () => {
    try {
      const [nextBranch, nextMessages] = await Promise.all([officeApi.branch(room.projectId), officeApi.roomMessages(room.projectId).catch(() => null)]);
      // Name custom roles anywhere in the room.
      if (nextBranch) registerTeam(teamOf(nextBranch.branch.runtime));
      setBranchView(nextBranch);
      if (nextMessages) setRoomMessages((current) => mergeRoomMessages(current, nextMessages));
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
      if (event.key === "Escape" && !openRequest && !openWorkflowRequest && !boardTab && !teamConfigOpen) {
        if (stageTab !== null) setStageTab(null);
        else if (selectedWorkerId) setSelectedWorkerId(null);
        else onExit();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openRequest, openWorkflowRequest, boardTab, teamConfigOpen, stageTab, selectedWorkerId, onExit]);

  // Sessions a role handed off from stay readable, but only current ones take a desk.
  const replacedIds = useMemo(
    () => new Set((branchView?.roleSessions ?? []).filter((link) => link.retiredAt !== null).map((link) => link.sessionId)),
    [branchView],
  );
  const currentWorkers = useMemo(() => workers.filter((worker) => !replacedIds.has(worker.id)), [workers, replacedIds]);
  const replacedWorkers = workers.filter((worker) => replacedIds.has(worker.id));
  const layout = useMemo(() => buildRoomLayout(currentWorkers.length), [currentWorkers.length]);
  const healthBySession = useMemo(
    () => new Map((branchView?.health ?? []).filter((entry) => entry.sessionId).map((entry) => [entry.sessionId as string, entry])),
    [branchView],
  );
  const stateOf = useCallback((worker: WorkerSession) => roomWorkerState(worker, healthBySession.get(worker.id)), [healthBySession]);
  const selectedWorker = workers.find((worker) => worker.id === selectedWorkerId) ?? null;
  // Choosing a worker anywhere in the room shows it in the Office Board, even when collapsed.
  useEffect(() => {
    if (selectedWorkerId) setPaneOpen("left", true);
  }, [selectedWorkerId, setPaneOpen]);
  // Messages go to the chosen worker, otherwise to the PM, which coordinates an SDLC room.
  const [recipientId, setRecipientId] = useState<string | null>(null);
  const pmSessionId = branchView?.roleSessions.find((link) => link.role === "pm" && !link.temporary && link.retiredAt === null)?.sessionId ?? null;
  const recipient = workers.some((worker) => worker.id === recipientId) ? recipientId : (pmSessionId ?? currentWorkers[0]?.id ?? null);
  // Live temporary workers and whether their task is done, so the PO can close them.
  const temporaries = useMemo(() => {
    const map = new Map<string, TemporaryWorker>();
    for (const link of branchView?.roleSessions ?? []) {
      if (!link.temporary || link.retiredAt !== null) continue;
      const task = branchView?.tasks.find((entry) => entry.id === link.taskId);
      map.set(link.sessionId, { task: task?.title ?? null, finished: !task || task.status === "done" || task.status === "cancelled" });
    }
    return map;
  }, [branchView]);

  // Closing stops and archives sessions in Omnigent, so they leave the room's lists.
  const closeSessions = useCallback(
    async (kind: "temporary" | "replaced", sessionIds: string[]): Promise<string | null> => {
      if (!branchView) return null;
      try {
        const close = kind === "temporary" ? officeApi.closeTemporaryWorkers : officeApi.closeReplacedSessions;
        const result = await close(branchView.branch.id, sessionIds);
        if (selectedWorkerId && result.closed.includes(selectedWorkerId)) setSelectedWorkerId(null);
        if (recipientId && result.closed.includes(recipientId)) setRecipientId(null);
        await load();
        return result.failed.length ? `Could not close: ${result.failed.join("; ")}` : null;
      } catch (error) {
        return (error as Error).message;
      }
    },
    [branchView, selectedWorkerId, recipientId, load],
  );
  const closeTemporary = useCallback((sessionIds: string[]) => closeSessions("temporary", sessionIds), [closeSessions]);
  const closeReplaced = useCallback((sessionIds: string[]) => closeSessions("replaced", sessionIds), [closeSessions]);

  const mentionTargets = useMemo(
    () =>
      branchView
        ? roleTargets(
            teamOf(branchView.branch.runtime),
            (role) =>
              branchView.roleSessions.find((link) => link.role === role && !link.temporary && link.retiredAt === null)?.sessionId ?? null,
          )
        : sessionTargets(currentWorkers.map((worker) => ({ id: worker.id, name: workerName(worker) }))),
    [branchView, currentWorkers],
  );
  useEffect(() => {
    if (selectedWorkerId) setRecipientId(selectedWorkerId);
  }, [selectedWorkerId]);
  const explanation = explainRank(room, allRooms, serverNow, placementWaiting);
  const pendingWorkflow = workflowRequests(branchView);
  const openQuestion = branchView?.interviews.find((question) => question.status === "asked") ?? null;
  const humanTasks = (branchView?.tasks ?? []).filter((task) => task.status === "blocked" && task.responsible.includes("po"));
  const problems = branchView ? openProblems(branchView) : [];
  const waitingOnYou = requests.length + pendingWorkflow.length + (openQuestion ? 1 : 0) + humanTasks.length;
  const nothingPending = waitingOnYou === 0 && problems.length === 0;
  const working = currentWorkers.filter((worker) => stateOf(worker) === "working").length;
  const status: RoomStatus = problems.length
    ? { label: `${problems.length} stuck`, tone: "problem" }
    : waitingOnYou
      ? { label: `${waitingOnYou} waiting on you`, tone: "attention" }
      : working
        ? { label: `${working} working`, tone: "busy" }
        : { label: "Idle", tone: "ok" };
  // The service sends the latest 100 events.
  const eventCount = branchView ? (branchView.events.length >= 100 ? "100+" : String(branchView.events.length)) : null;

  return (
    <div className={`room-view room-grid${collapsed.has("left") ? " left-collapsed" : ""}${collapsed.has("right") ? " right-collapsed" : ""}`}>
      {collapsed.has("left") ? (
        <PaneRail side="left" label="Office Board" badge={String(currentWorkers.length)} onExpand={() => setPaneOpen("left", true)} />
      ) : (
        <OfficeBoard
          current={currentWorkers}
          replaced={replacedWorkers}
          stateOf={stateOf}
          selected={selectedWorker}
          onSelect={setSelectedWorkerId}
          onCollapse={() => setPaneOpen("left", false)}
          temporaries={temporaries}
          onCloseTemporary={closeTemporary}
          onCloseReplaced={closeReplaced}
        />
      )}
      <main className="room-center">
        <ProjectBar
          room={room}
          themed={Boolean(branchView)}
          status={status}
          partyCount={currentWorkers.length + 1}
          eventCount={eventCount}
          explanation={explanation}
          onExit={onExit}
          onOpenBoard={branchView ? () => setBoardTab("requirements") : null}
          onOpenTeam={() => {
            // An SDLC room shows each role's full configuration; otherwise the Team pane.
            if (branchView) {
              setTeamConfigOpen(true);
              return;
            }
            setPaneOpen("right", true);
            showPane("team");
            focusAfterRender("room-pane-tab-team");
          }}
          onUpdateRoom={onUpdateRoom}
        />
        <ProjectLinks projectId={room.projectId} name={room.name} />
        {/* The project bar's status already says when nothing is waiting. */}
        <section aria-label="Requests needing your decision" className="requests" hidden={nothingPending}>
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
                      : `Board proposal from the ${coordinatorName()} (${request.proposal.requirements.length} requirements)`
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
                  onClick={() => {
                    setBoardTab("interview");
                    focusAfterRender("interview-answer");
                  }}
                >
                  ⚑ {coordinatorName()} interview question: {openQuestion.question.slice(0, 90)}
                </button>
              </li>
            ) : null}
            {humanTasks.map((task) => (
              <li key={task.id}>
                <button
                  type="button"
                  className="request-button"
                  onClick={() => setBoardTab("tasks")}
                >
                  ⚑ Task waiting on you: {task.title}
                </button>
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
        {branchView && (branchView.health ?? []).some((entry) => entry.problem) ? (
          <AgentProblems view={branchView} onChanged={() => void load()} />
        ) : null}
        <section className="room-stage-area" aria-label="Office and files">
          <div className="stage-bar">
            <div role="tablist" aria-label="Office and open files" className="workflow-tabs stage-tabs">
              <button
                id="stage-tab-office"
                type="button"
                role="tab"
                aria-selected={showingOffice}
                aria-controls="stage-panel"
                className="workflow-tab"
                onClick={() => setStageTab(null)}
              >
                {animation ? "🏢 Office" : "💬 Chat"}
              </button>
              {openFiles.map((path) => (
                <span key={path} className={`stage-file-tab${stageTab === path ? " stage-file-tab-active" : ""}`}>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={stageTab === path}
                    aria-controls="stage-panel"
                    className="workflow-tab"
                    title={path}
                    onClick={() => setStageTab(path)}
                  >
                    {path.split("/").pop()}
                  </button>
                  <button type="button" className="stage-tab-close" aria-label={`Close ${path}`} onClick={() => closeFile(path)}>
                    ×
                  </button>
                </span>
              ))}
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={animation}
              className={`animation-toggle${animation ? " animation-on" : ""}`}
              title={animation ? "Switch to a chat view of the room" : "Switch back to the pixel-art office"}
              onClick={toggleAnimation}
            >
              Animation <span className="animation-state">{animation ? "ON" : "OFF"}</span>
            </button>
          </div>
          <div id="stage-panel" role="tabpanel" className="room-stage-panel">
            {/* The office stays mounted behind file tabs so its walker keeps its place. */}
            {animation ? (
              <div className="room-stage" hidden={!showingOffice}>
                <RoomCanvas
                  layout={layout}
                  workers={currentWorkers}
                  stateOf={stateOf}
                  selectedWorkerId={selectedWorkerId}
                  onOpenWorker={setSelectedWorkerId}
                  onExit={onExit}
                  inputEnabled={showingOffice && !openRequest && !openWorkflowRequest && !boardTab && !teamConfigOpen}
                />
              </div>
            ) : showingOffice ? (
              <SlackView
                roomName={room.name}
                roomMessages={roomMessages}
                view={branchView}
                workers={currentWorkers}
                stateOf={stateOf}
                recipientId={recipient}
                onRecipientChange={setRecipientId}
                temporaries={temporaries}
                onCloseTemporary={closeTemporary}
              />
            ) : null}
            {stageTab !== null ? <FileViewer key={stageTab} projectId={room.projectId} path={stageTab} onOpenFile={openFile} /> : null}
          </div>
        </section>
        <MessageComposer
          workers={selectedWorker && !currentWorkers.includes(selectedWorker) ? [...currentWorkers, selectedWorker] : currentWorkers}
          recipientId={recipient}
          onRecipientChange={setRecipientId}
          stateOf={stateOf}
          targets={mentionTargets}
          projectId={room.projectId}
          onSent={(message) => setRoomMessages((current) => mergeRoomMessages(current, [message]))}
        />
        {!branchView && branchLoaded ? <ThemeSetup room={room} onAssigned={() => void load()} /> : null}
      </main>
      {collapsed.has("right") ? (
        <PaneRail
          side="right"
          label="Team · Files"
          badge={waitingOnYou ? `${waitingOnYou} waiting` : null}
          onExpand={() => setPaneOpen("right", true)}
        />
      ) : (
        <aside className="room-pane room-right" aria-label="Team and files">
          <div className="pane-header">
            <div role="tablist" aria-label="Room details" className="workflow-tabs">
              {(["team", "files"] as const).map((pane) => (
                <button
                  key={pane}
                  id={`room-pane-tab-${pane}`}
                  type="button"
                  role="tab"
                  aria-selected={rightPane === pane}
                  aria-controls="room-pane-panel"
                  className="workflow-tab"
                  onClick={() => showPane(pane)}
                >
                  {pane === "team" ? "Team" : "Files"}
                </button>
              ))}
            </div>
            <CollapseButton side="right" label="Team and Files" onCollapse={() => setPaneOpen("right", false)} />
          </div>
          <div role="tabpanel" id="room-pane-panel" aria-labelledby={`room-pane-tab-${rightPane}`} className="room-pane-body">
            {rightPane === "team" ? (
              <TeamPane
                projectId={room.projectId}
                view={branchView}
                workers={currentWorkers}
                stateOf={stateOf}
                waitingOnYou={waitingOnYou}
                onChanged={() => void load()}
                onOpenWorker={setSelectedWorkerId}
              />
            ) : (
              <FilesPane projectId={room.projectId} hasFolder={Boolean(room.hostId && room.workspace)} onOpenFile={openFile} />
            )}
          </div>
        </aside>
      )}
      {teamConfigOpen && branchView ? (
        <TeamConfigModal
          view={branchView}
          roomName={room.name}
          workers={currentWorkers}
          stateOf={stateOf}
          onChanged={() => void load()}
          onClose={() => setTeamConfigOpen(false)}
        />
      ) : null}
      {boardTab && branchView ? (
        <BoardModal view={branchView} tab={boardTab} onTabChange={setBoardTab} onChanged={() => void load()} onClose={() => setBoardTab(null)} />
      ) : null}
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
