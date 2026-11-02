import { useEffect, useState } from "react";
import type { RoleToolReport } from "../../shared/roleTools.ts";
import { roleAgentName, roleTitle, type AgentRoleId } from "../../shared/sdlcTheme.ts";
import { teamOf, coordinatorName } from "../../shared/team.ts";
import { READINESS_LABELS, type RoleHealth } from "../../shared/troubleshoot.ts";
import type { BranchView } from "../../shared/workflowTypes.ts";
import { type WorkerSession, officeApi } from "../api.ts";
import { PersonSprite } from "../PersonSprite.tsx";
import { HUMAN_PALETTE, workerPalette } from "../sprites.ts";
import { WORKER_STATE_LABELS, type WorkerState } from "../workers.ts";
import { workerName } from "./OfficeBoard.tsx";
import { SwitchHarnessDialog } from "./workflow/SwitchHarnessDialog.tsx";
import { Activity, RoleTools, focusProblem, roleHealth, timeAgo, useRoleWork } from "./workflow/WorkflowSections.tsx";

// Matches the office service's session refresh, so the total follows a running turn.
const USAGE_POLL_MS = 4_000;

function compactNumber(value: number): string {
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function useProjectUsage(projectId: string) {
  const [usage, setUsage] = useState<{ totalTokens: number; costUsd: number; sessions: number } | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      officeApi
        .roomUsage(projectId)
        .then((next) => !cancelled && setUsage(next))
        .catch(() => undefined);
    void load();
    const timer = window.setInterval(load, USAGE_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [projectId]);
  return usage;
}

function useRoleTools(branchId: string | null, sessionKey: string) {
  const [tools, setTools] = useState<RoleToolReport[] | null>(null);
  useEffect(() => {
    if (!branchId) return;
    let cancelled = false;
    officeApi
      .roleTools(branchId)
      .then((reports) => !cancelled && setTools(reports))
      .catch(() => !cancelled && setTools([]));
    return () => {
      cancelled = true;
    };
  }, [branchId, sessionKey]);
  return tools;
}

function StateLabel({ state, missing }: { state: WorkerState | null; missing: boolean }) {
  return (
    <span className="team-state">
      <span className={`state-dot state-${state ?? "not_started"}`} aria-hidden="true" />
      {state ? WORKER_STATE_LABELS[state] : missing ? "Session missing" : "Not started"}
    </span>
  );
}

interface TeamPaneProps {
  projectId: string;
  view: BranchView | null;
  /** Current (not replaced) sessions in the room. */
  workers: WorkerSession[];
  stateOf: (worker: WorkerSession) => WorkerState;
  /** Requests, gates, questions, and tasks waiting on the PO. */
  waitingOnYou: number;
  onChanged: () => void;
  onOpenWorker: (sessionId: string) => void;
}

/** Project token use, the team including the PO, and the room's activity. */
export function TeamPane({ projectId, view, workers, stateOf, waitingOnYou, onChanged, onOpenWorker }: TeamPaneProps) {
  const usage = useProjectUsage(projectId);
  const work = useRoleWork(view?.branch.id ?? null);
  const [notice, setNotice] = useState<string | null>(null);
  const [changing, setChanging] = useState<RoleHealth | null>(null);
  const [openTools, setOpenTools] = useState<AgentRoleId | null>(null);
  const live = (view?.roleSessions ?? []).filter((link) => link.retiredAt === null);
  // Reload tools when a role starts or restarts in a new session.
  const tools = useRoleTools(
    view?.branch.id ?? null,
    live
      .filter((link) => !link.temporary)
      .map((link) => link.sessionId)
      .join(","),
  );

  const restart = (role: AgentRoleId, started: boolean) => {
    if (!view) return;
    void officeApi
      .rollover(view.branch.id, role, started ? "PO restarted the role." : "PO started the role.")
      .then(() => {
        setNotice(started ? `${roleTitle(role)} restarted with a recorded handoff.` : `${roleTitle(role)} started.`);
        onChanged();
      })
      .catch((caught: Error) => setNotice(caught.message));
  };

  return (
    <div className="team-pane">
      <section aria-label="Token usage" className="usage-card">
        <h3>Tokens used in this project</h3>
        {usage ? (
          <p>
            <span className="usage-total">{compactNumber(usage.totalTokens)}</span>{" "}
            <span className="muted small">
              tokens · ${usage.costUsd.toFixed(2)} · across {usage.sessions} session{usage.sessions === 1 ? "" : "s"}
            </span>
          </p>
        ) : (
          <p className="muted small">Counting…</p>
        )}
      </section>

      <section aria-label="Team">
        <h3>Team</h3>
        <ul className="team-list">
          <li className="team-member">
            <PersonSprite palette={HUMAN_PALETTE} />
            <div className="team-member-body">
              <div className="team-member-head">
                <strong>{view ? "Product Owner (you)" : "You"}</strong>
                <span className="team-state">
                  <span className={`state-dot state-${waitingOnYou ? "waiting_human" : "idle"}`} aria-hidden="true" />
                  {waitingOnYou ? `${waitingOnYou} waiting on you` : "Nothing waiting"}
                </span>
              </div>
              <div className="muted small">Human · {view ? `approves gates, owns the board, answers the ${coordinatorName()}` : "directs each session"}</div>
            </div>
          </li>
          {view
            ? teamOf(view.branch.runtime).roles.map(({ id: role }) => {
                const health = roleHealth(view, role);
                const links = live.filter((link) => link.role === role);
                const started = links.some((link) => !link.temporary);
                const temporary = links.length - (started ? 1 : 0);
                const session = workers.find((worker) => worker.id === health?.sessionId);
                const state = session ? stateOf(session) : null;
                const line = work.get(role);
                const report = tools?.find((entry) => entry.role === role);
                const toolsOpen = openTools === role;
                const notReady = health && health.harnessReadiness !== "ready" && health.harnessReadiness !== "unknown";
                return (
                  <li key={role} className="team-member">
                    <PersonSprite palette={workerPalette(roleAgentName(role))} walking={state === "working"} />
                    <div className="team-member-body">
                      <div className="team-member-head">
                        {session ? (
                          <button type="button" className="link-button team-member-name" onClick={() => onOpenWorker(session.id)}>
                            {roleTitle(role)}
                          </button>
                        ) : (
                          <strong>{roleTitle(role)}</strong>
                        )}
                        {temporary > 0 ? <span className="muted small">+{temporary} temporary</span> : null}
                        <StateLabel state={state} missing={Boolean(health?.sessionId)} />
                      </div>
                      {health ? (
                        <div className="muted small">
                          {health.harness} · {health.model ?? "default model"}
                          {health.effort ? ` · ${health.effort}` : ""}
                          {notReady ? <span className="error-text"> · on this host: {READINESS_LABELS[health.harnessReadiness]}</span> : null}
                        </div>
                      ) : null}
                      {health?.problem && !health.dismissed ? (
                        <button type="button" className="link-button" onClick={() => focusProblem(view, health)}>
                          ⚠ {health.problem.title}
                        </button>
                      ) : null}
                      <div className={`team-work team-work-${line?.kind ?? "idle"}`} title={line?.text}>
                        ↳ {line ? line.text : "…"}
                        {line?.at ? <span className="muted small"> · {timeAgo(line.at)}</span> : null}
                      </div>
                      <div className="team-member-actions">
                        <button
                          type="button"
                          disabled={!health}
                          aria-label={`Change the ${roleTitle(role)}'s harness, model, or effort`}
                          onClick={() => health && setChanging(health)}
                        >
                          Change
                        </button>
                        {started || role === "pm" ? (
                          <button type="button" onClick={() => restart(role, started)}>
                            {started ? "Restart" : `Start ${coordinatorName()}`}
                          </button>
                        ) : null}
                        {report ? (
                          <button
                            type="button"
                            className="link-button"
                            aria-expanded={toolsOpen}
                            aria-controls={`role-tools-${role}`}
                            title={`${report.officeTools.length} office tools, ${report.mcpServers.length} MCP servers, ${report.skills.length} skills, ${report.policies.length} policies`}
                            onClick={() => setOpenTools(toolsOpen ? null : role)}
                          >
                            {toolsOpen ? "▾" : "▸"} {report.officeTools.length} tools
                          </button>
                        ) : null}
                      </div>
                      {toolsOpen && report ? (
                        <div id={`role-tools-${role}`}>
                          <RoleTools report={report} />
                        </div>
                      ) : null}
                    </div>
                  </li>
                );
              })
            : workers.map((worker) => (
                <li key={worker.id} className="team-member">
                  <PersonSprite palette={workerPalette(worker.agent_name ?? worker.id)} walking={stateOf(worker) === "working"} />
                  <div className="team-member-body">
                    <div className="team-member-head">
                      <button type="button" className="link-button team-member-name" onClick={() => onOpenWorker(worker.id)}>
                        {workerName(worker)}
                      </button>
                      <StateLabel state={stateOf(worker)} missing={false} />
                    </div>
                    <div className="muted small">Generalist · {worker.agent_name ?? "session"}</div>
                  </div>
                </li>
              ))}
        </ul>
        {view ? (
          <>
            <p className="muted small">Roles without a session start when the {coordinatorName()} first gives them work, on the harness and model shown.</p>
            <button
              type="button"
              className="danger"
              onClick={() =>
                void officeApi
                  .stopBranch(view.branch.id)
                  .then((result) => setNotice(`Stop sent to ${result.stopped} session(s).`))
                  .catch((caught: Error) => setNotice(caught.message))
              }
            >
              Stop all work in this room
            </button>
          </>
        ) : null}
        {notice ? <p role="status">{notice}</p> : null}
      </section>

      {view ? (
        <section aria-label="Activity" className="team-activity">
          <h3>Activity</h3>
          <Activity view={view} />
        </section>
      ) : null}

      {changing && view ? (
        <SwitchHarnessDialog
          branchId={view.branch.id}
          hostId={view.branch.runtime.hostId}
          target={changing}
          roles={(view.health ?? []).filter((entry) => !entry.temporary)}
          reason="PO changed the harness, model, or effort."
          onClose={() => setChanging(null)}
          onSwitched={(message) => {
            setChanging(null);
            setNotice(message);
            onChanged();
          }}
        />
      ) : null}
    </div>
  );
}
