import { useCallback, useEffect, useRef, useState } from "react";
import type { RoleToolReport } from "../../../shared/roleTools.ts";
import { GATE_TITLES, roleAgentName, type AgentRoleId, type GateType } from "../../../shared/sdlcTheme.ts";
import { type TeamDefinition, hasRole, sameTeam, teamOf, teamProblems, teamRole, coordinatorName } from "../../../shared/team.ts";
import { READINESS_LABELS, type RoleHealth } from "../../../shared/troubleshoot.ts";
import type { BranchView } from "../../../shared/workflowTypes.ts";
import { type WorkerSession, officeApi } from "../../api.ts";
import { useRuntimeOptions } from "../../capabilities.ts";
import { PersonSprite } from "../../PersonSprite.tsx";
import { workerPalette } from "../../sprites.ts";
import { WORKER_STATE_LABELS, type WorkerState } from "../../workers.ts";
import { RoleToolsEditor } from "./RoleToolsEditor.tsx";
import { SwitchHarnessDialog } from "./SwitchHarnessDialog.tsx";
import { RaciMatrixEditor, RoleDefinitionEditor, RoleRaciEditor, RoleTasks, TasksByRole, TeamActions, TeamNav } from "./TeamEditing.tsx";
import { TeamTemplates } from "./TeamTemplates.tsx";
import { roleHealth } from "./WorkflowSections.tsx";

interface RoleConfig {
  role: AgentRoleId;
  prompt: string;
  customInstructions: string;
}

interface TeamConfigModalProps {
  view: BranchView;
  roomName: string;
  workers: WorkerSession[];
  stateOf: (worker: WorkerSession) => WorkerState;
  onChanged: () => void;
  onClose: () => void;
}

/** The room's team in one place: its roles, task types, and RACI, and each role's runtime, session, tools, and prompt. */
export function TeamConfigModal({ view, roomName, workers, stateOf, onChanged, onClose }: TeamConfigModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const omnigentUrl = useRuntimeOptions()?.omnigentUrl;
  // "team" shows the whole team; otherwise a role id.
  const [selected, setSelected] = useState<string>("team");
  const [configs, setConfigs] = useState<RoleConfig[] | null>(null);
  const [tools, setTools] = useState<RoleToolReport[] | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [changing, setChanging] = useState<RoleHealth | null>(null);
  const branchId = view.branch.id;
  const savedTeam = teamOf(view.branch.runtime);
  const [teamDraft, setTeamDraft] = useState<TeamDefinition>(savedTeam);
  const savedKey = JSON.stringify(savedTeam);
  // A save or a template switch replaces the room's team; start the draft from it again.
  useEffect(() => {
    setTeamDraft(JSON.parse(savedKey) as TeamDefinition);
  }, [savedKey]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal?.();
  }, []);

  const loadConfigs = useCallback(
    () =>
      officeApi
        .teamConfig(branchId)
        .then(setConfigs)
        .catch((caught: Error) => setNotice({ text: caught.message, error: true })),
    [branchId],
  );

  const loadTools = useCallback(
    () =>
      officeApi
        .roleTools(branchId)
        .then(setTools)
        .catch(() => setTools([])),
    [branchId],
  );

  // Loaded once per open; saving reloads what changed.
  useEffect(() => {
    void loadConfigs();
    void loadTools();
  }, [loadConfigs, loadTools]);

  const role = selected === "team" ? null : selected;
  const draftRole = role ? teamRole(teamDraft, role) : undefined;
  const savedRole = role ? hasRole(savedTeam, role) : false;
  const config = configs?.find((entry) => entry.role === role) ?? null;
  // Start editing from the saved instructions whenever another role is chosen or they reload.
  useEffect(() => {
    setDraft(config?.customInstructions ?? "");
  }, [role, config?.customInstructions]);

  const health = role ? roleHealth(view, role) : undefined;
  const session = workers.find((worker) => worker.id === health?.sessionId) ?? null;
  const state = session ? stateOf(session) : null;
  const started = Boolean(health?.sessionId);
  const report = tools?.find((entry) => entry.role === role) ?? null;
  const dirty = config !== null && draft.trim() !== config.customInstructions.trim();
  const teamDirty = !sameTeam(teamDraft, savedTeam);
  const gates = (Object.keys(view.branch.gates) as GateType[]).filter((gate) => view.branch.gates[gate]);
  const title = draftRole?.title ?? role ?? "";

  const run = async (action: () => Promise<string>) => {
    setBusy(true);
    setNotice(null);
    try {
      setNotice({ text: await action(), error: false });
    } catch (caught) {
      setNotice({ text: (caught as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const saveTeam = () =>
    run(async () => {
      const removed = savedTeam.roles.filter((entry) => !hasRole(teamDraft, entry.id)).map((entry) => entry.title);
      await officeApi.setTeam(branchId, teamDraft);
      await Promise.all([loadConfigs(), loadTools()]);
      onChanged();
      return `Team saved. New tasks follow it.${removed.length ? ` ${removed.join(", ")} left the team; their sessions were stopped.` : ""}`;
    });

  const saveInstructions = (restart: boolean) =>
    run(async () => {
      await officeApi.setRoleInstructions(branchId, role as string, draft, restart);
      await loadConfigs();
      onChanged();
      return restart ? `${title} restarted with the new instructions.` : started ? "Saved. The role uses them when it next restarts." : "Saved. The role starts with them.";
    });

  const restart = () =>
    run(async () => {
      await officeApi.rollover(branchId, role as string, started ? "PO restarted the role." : "PO started the role.");
      onChanged();
      return started ? `${title} restarted with a recorded handoff.` : `${title} started.`;
    });

  const navRoles = teamDraft.roles.map((entry) => {
    const entryHealth = roleHealth(view, entry.id);
    const entrySession = workers.find((worker) => worker.id === entryHealth?.sessionId);
    const entryState = entrySession ? stateOf(entrySession) : null;
    return {
      id: entry.id,
      title: entry.title,
      state: entryState ?? "not_started",
      stateLabel: entryState ? WORKER_STATE_LABELS[entryState] : "Not started",
      marked: Boolean(configs?.find((item) => item.role === entry.id)?.customInstructions),
      unsaved: !hasRole(savedTeam, entry.id),
    };
  });

  const noticeLine = notice ? (
    <p className={`small${notice.error ? " error-text" : " muted"}`} role="status">
      {notice.text}
    </p>
  ) : null;

  return (
    <dialog
      ref={dialogRef}
      className="approval-dialog team-config"
      aria-labelledby="team-config-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!changing) onClose();
      }}
    >
      <header className="team-config-header">
        <h2 id="team-config-title">Team configuration</h2>
        <span className="muted small">
          {view.branch.runtime.hostId ? `Host folder ${view.branch.runtime.workspace ?? "—"}` : `Runner ${view.branch.runtime.runnerId ?? "—"}`}
          {gates.length ? ` · Gates: ${gates.map((gate) => GATE_TITLES[gate]).join(", ")}` : " · No human gates"}
          {teamDirty ? " · unsaved team changes" : ""}
        </span>
        <button type="button" onClick={onClose} aria-label="Close team configuration">
          ✕
        </button>
      </header>
      <TeamTemplates
        view={view}
        roomName={roomName}
        startedRoles={new Set((view.health ?? []).filter((entry) => !entry.temporary && entry.sessionId).map((entry) => entry.role))}
        onApplied={(message) => {
          setNotice({ text: message, error: false });
          void loadConfigs();
          void loadTools();
          onChanged();
        }}
      />
      <div className="team-config-body">
        <TeamNav roles={navRoles} selected={selected} onSelect={setSelected} team={teamDraft} onTeamChange={setTeamDraft} />
        {role === null || !draftRole ? (
          <section className="team-config-detail" aria-label="Whole team configuration">
            <div className="team-config-title">
              <h3>Whole team</h3>
            </div>
            <h4>
              RACI and task types <span className="muted small">· new tasks follow them; existing tasks keep their roles</span>
            </h4>
            <RaciMatrixEditor team={teamDraft} saved={savedTeam} onChange={setTeamDraft} />
            <TeamActions saved={savedTeam} draft={teamDraft} onDraftChange={setTeamDraft} onSave={saveTeam} busy={busy} />
            {noticeLine}
            <TasksByRole view={view} team={teamDraft} />
          </section>
        ) : (
          <section className="team-config-detail" aria-label={`${title} configuration`}>
            <div className="team-config-title">
              <PersonSprite palette={workerPalette(roleAgentName(role))} walking={state === "working"} />
              <h3>{title}</h3>
              <span className="team-state">
                <span className={`state-dot state-${state ?? "not_started"}`} aria-hidden="true" />
                {state ? WORKER_STATE_LABELS[state] : started ? "Session missing" : savedRole ? "Not started" : "Not saved yet"}
              </span>
            </div>

            <h4>Role</h4>
            <RoleDefinitionEditor
              team={teamDraft}
              role={draftRole}
              hostId={view.branch.runtime.hostId}
              onChange={setTeamDraft}
              onRemoved={() => setSelected("team")}
            />

            <h4>Tasks and RACI</h4>
            <RoleTasks view={view} team={teamDraft} role={role} />
            <RoleRaciEditor team={teamDraft} saved={savedTeam} role={role} onChange={setTeamDraft} />
            <TeamActions saved={savedTeam} draft={teamDraft} onDraftChange={setTeamDraft} onSave={saveTeam} busy={busy} />
            {noticeLine}

            {!savedRole ? (
              <p className="muted small">
                Save the team to give the {title} its runtime, tools, and instructions. {teamProblems(teamDraft).length ? "Fix the problems above first." : ""}
              </p>
            ) : (
              <>
                <h4>Runtime</h4>
                <dl className="team-config-facts">
                  <dt>Harness</dt>
                  <dd>
                    {health?.harness ?? "—"}
                    {health && health.harnessReadiness !== "unknown" ? (
                      <span className={health.harnessReadiness === "ready" ? "muted" : "error-text"}> · {READINESS_LABELS[health.harnessReadiness]} on this host</span>
                    ) : null}
                  </dd>
                  <dt>Model</dt>
                  <dd>{health?.model ?? "Harness default"}</dd>
                  <dt>Effort</dt>
                  <dd>{health?.effort ?? "Harness default"}</dd>
                </dl>
                <div className="team-config-actions">
                  <button type="button" disabled={!health || busy} onClick={() => health && setChanging(health)}>
                    Change harness, model, or effort…
                  </button>
                </div>

                <h4>Session</h4>
                <dl className="team-config-facts">
                  <dt>Omnigent session</dt>
                  <dd>
                    {health?.sessionId ? (
                      <>
                        <code>{health.sessionId}</code>
                        {omnigentUrl ? (
                          <>
                            {" "}
                            <a href={`${omnigentUrl.replace(/\/$/, "")}/c/${encodeURIComponent(health.sessionId)}`} target="_blank" rel="noreferrer">
                              Open in Omnigent
                            </a>
                          </>
                        ) : null}
                      </>
                    ) : (
                      "Starts when the PM first gives it work"
                    )}
                  </dd>
                </dl>
                <div className="team-config-actions">
                  {started || role === "pm" ? (
                    <button type="button" disabled={busy} onClick={() => void restart()}>
                      {started ? "Restart with handoff" : `Start ${coordinatorName()}`}
                    </button>
                  ) : null}
                </div>

                <h4>Tools and policies</h4>
                {report ? (
                  <RoleToolsEditor
                    report={report}
                    roleName={title}
                    canRestart={started || role === "pm"}
                    onSave={(settings, restartRole) =>
                      run(async () => {
                        await officeApi.setRoleToolSettings(branchId, role, settings, restartRole);
                        await loadTools();
                        onChanged();
                        return restartRole ? `${title} restarted with its new tools and policies.` : "Tools and policies saved.";
                      })
                    }
                  />
                ) : (
                  <p className="muted small">{tools === null ? "Loading…" : "Not available."}</p>
                )}

                <h4>Role prompt</h4>
                <label className="team-config-instructions">
                  <span>
                    Your instructions for the {title} <span className="muted small">(added to the end of its prompt)</span>
                  </span>
                  <textarea
                    rows={4}
                    maxLength={8000}
                    value={draft}
                    placeholder={`For example: "Always add a regression test for each fix."`}
                    onChange={(event) => setDraft(event.target.value)}
                  />
                </label>
                <div className="team-config-actions">
                  <button type="button" className="primary" disabled={!dirty || busy} onClick={() => void saveInstructions(false)}>
                    Save
                  </button>
                  {started || role === "pm" ? (
                    <button type="button" disabled={!dirty || busy} onClick={() => void saveInstructions(true)}>
                      Save and restart
                    </button>
                  ) : null}
                  {dirty ? (
                    <button type="button" disabled={busy} onClick={() => setDraft(config?.customInstructions ?? "")}>
                      Discard
                    </button>
                  ) : null}
                </div>
                <details className="team-config-prompt" open>
                  <summary>
                    Full prompt the office gives the {title} <span className="muted small">(as a new session gets it)</span>
                  </summary>
                  <pre>{config?.prompt ?? "Loading…"}</pre>
                </details>
                <p className="muted small">Omnigent adds its own runtime instructions (tools, policies) on top of this prompt.</p>
              </>
            )}
          </section>
        )}
      </div>
      {changing ? (
        <SwitchHarnessDialog
          branchId={branchId}
          hostId={view.branch.runtime.hostId}
          target={changing}
          roles={(view.health ?? []).filter((entry) => !entry.temporary)}
          reason="PO changed the harness, model, or effort."
          onClose={() => setChanging(null)}
          onSwitched={(message) => {
            setChanging(null);
            setNotice({ text: message, error: false });
            onChanged();
          }}
        />
      ) : null}
    </dialog>
  );
}
