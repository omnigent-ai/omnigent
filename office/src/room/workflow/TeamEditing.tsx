// Building blocks of the Team configuration layout, shared by a live room's Team
// configuration dialog and the New room screen: the roles list, the RACI matrix with its task
// types, a role's RACI, a role's definition, and the room's tasks by role.

import { useState } from "react";
import { COORDINATOR_ROLE, GATE_TITLES, type GateType, type Raci, roleAgentName } from "../../../shared/sdlcTheme.ts";
import {
  PO_ROLE,
  RACI_LABELS,
  RACI_LETTERS,
  type TeamDefinition,
  type TeamRole,
  addBuiltInRole,
  addRole,
  addTaskType,
  assign,
  raciParticipants,
  removeRole,
  removeTaskType,
  sameTeam,
  sdlcTeam,
  taskTypeOf,
  teamProblems,
  updateRole,
  updateTaskType,
} from "../../../shared/team.ts";
import type { BranchView } from "../../../shared/workflowTypes.ts";
import { PersonSprite } from "../../PersonSprite.tsx";
import { HarnessSelect } from "../../RuntimePickers.tsx";
import { HUMAN_PALETTE, workerPalette } from "../../sprites.ts";

export const LETTER_NAMES: Record<Raci, string> = { R: "R", A: "A", RA: "R+A", C: "C", I: "I" };
const OPEN_STATUSES = new Set(["pending", "in_progress", "blocked", "submitted", "awaiting_gate"]);

type Task = BranchView["tasks"][number];

/** A role's tasks with its part in each: responsible, accountable, both, or consulted. */
export function tasksFor(view: BranchView, team: TeamDefinition, role: string): { open: { task: Task; part: string }[]; done: number } {
  const mine = view.tasks.flatMap((task) => {
    const responsible = task.responsible.includes(role);
    const accountable = task.accountable === role;
    const consulted = taskTypeOf(team, task.taskType)?.assignments[role] === "C";
    if (!responsible && !accountable && !consulted) return [];
    return [{ task, part: accountable ? (responsible ? "R+A" : "A") : responsible ? "R" : "C" }];
  });
  return { open: mine.filter((entry) => OPEN_STATUSES.has(entry.task.status)), done: mine.filter((entry) => entry.task.status === "done").length };
}

export function TaskLine({ task, part }: { task: Task; part: string }) {
  return (
    <li>
      <span className="raci-letter" title={RACI_LABELS[part === "R+A" ? "RA" : (part as Raci)]}>
        {part}
      </span>
      <span className="role-task-title" title={task.title}>
        {task.title}
      </span>
      <span className="state-pill">{task.status.replace("_", " ")}</span>
    </li>
  );
}

export interface NavRole {
  id: string;
  title: string;
  /** e.g. a state dot class suffix; omitted for a draft team. */
  state?: string;
  stateLabel?: string;
  /** Saved custom instructions or other changes worth a mark. */
  marked?: boolean;
  /** On the draft team but not saved yet. */
  unsaved?: boolean;
}

interface TeamNavProps {
  roles: NavRole[];
  /** "team" for the whole team, or a role id. */
  selected: string;
  onSelect: (selection: string) => void;
  team: TeamDefinition;
  onTeamChange: (team: TeamDefinition) => void;
  /** Show "+ Add role"; off while a new room follows a template. */
  allowAdd?: boolean;
  /** Show a remove button on each role but the PM. */
  allowRemove?: boolean;
}

/** The left column: the whole team, each role, and adding or removing roles. */
export function TeamNav({ roles, selected, onSelect, team, onTeamChange, allowAdd = true, allowRemove = false }: TeamNavProps) {
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  const missingBuiltIns = sdlcTeam().roles.filter((role) => !team.roles.some((entry) => entry.id === role.id));

  const add = () => {
    if (!title.trim()) return;
    const result = addRole(team, title);
    onTeamChange(result.team);
    onSelect(result.roleId);
    setTitle("");
    setAdding(false);
  };

  return (
    <nav className="team-config-roles" aria-label="Roles">
      <button type="button" className="team-config-team" aria-current={selected === "team" || undefined} onClick={() => onSelect("team")}>
        <span className="team-config-role-name">Team</span>
        <span className="muted small">
          {team.roles.length} role{team.roles.length === 1 ? "" : "s"} · {team.taskTypes.length} task types
        </span>
      </button>
      {roles.map((role) => (
        <div key={role.id} className="team-nav-role">
          <button type="button" aria-current={selected === role.id || undefined} onClick={() => onSelect(role.id)}>
            <PersonSprite palette={workerPalette(roleAgentName(role.id))} pixel={2} />
            <span className="team-config-role-name">
              {role.title}
              {role.unsaved ? <span className="muted small"> · new</span> : null}
            </span>
            {role.marked ? (
              <span className="team-config-custom" title="Has your instructions">
                ✎
              </span>
            ) : null}
            {role.state ? <span className={`state-dot state-${role.state}`} aria-label={role.stateLabel ?? role.state} /> : null}
          </button>
          {allowRemove && role.id !== COORDINATOR_ROLE ? (
            <button
              type="button"
              className="team-nav-remove"
              aria-label={`Remove ${role.title} from the team`}
              title={`Remove ${role.title} from the team`}
              onClick={() => {
                onTeamChange(removeRole(team, role.id));
                if (selected === role.id) onSelect("team");
              }}
            >
              ×
            </button>
          ) : null}
        </div>
      ))}
      {!allowAdd ? null : adding ? (
        // Not a <form>: these fields also sit inside the New room form, and forms cannot nest.
        <div className="add-role-form" role="group" aria-label="Add a role">
          <input
            aria-label="New role title"
            placeholder="Role title, e.g. Designer"
            maxLength={40}
            value={title}
            autoFocus
            onChange={(event) => setTitle(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                add();
              }
            }}
          />
          <div className="team-config-actions">
            <button type="button" className="primary" disabled={!title.trim()} onClick={add}>
              Add
            </button>
            <button type="button" onClick={() => setAdding(false)}>
              Cancel
            </button>
          </div>
          {missingBuiltIns.length ? (
            <div className="small">
              <span className="muted">Or add back: </span>
              {missingBuiltIns.map((role) => (
                <button
                  key={role.id}
                  type="button"
                  className="link-button"
                  onClick={() => {
                    onTeamChange(addBuiltInRole(team, role.id));
                    onSelect(role.id);
                    setAdding(false);
                  }}
                >
                  {role.title}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      ) : (
        <button type="button" className="add-role-button" onClick={() => setAdding(true)}>
          + Add role
        </button>
      )}
    </nav>
  );
}

interface TeamActionsProps {
  saved: TeamDefinition;
  draft: TeamDefinition;
  onDraftChange: (team: TeamDefinition) => void;
  /** Absent while the team is a draft of a room not created yet. */
  onSave?: () => Promise<void>;
  busy: boolean;
}

/** What stops the draft team from running, and Save / Discard / Reset. */
export function TeamActions({ saved, draft, onDraftChange, onSave, busy }: TeamActionsProps) {
  const dirty = !sameTeam(draft, saved);
  const problems = teamProblems(draft);
  return (
    <>
      {problems.length ? (
        <ul className="error-text small raci-problems" role="alert">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      ) : null}
      <div className="team-config-actions">
        {onSave ? (
          <button type="button" className="primary" disabled={!dirty || problems.length > 0 || busy} onClick={() => void onSave()}>
            Save team
          </button>
        ) : null}
        {onSave && dirty ? (
          <button type="button" disabled={busy} onClick={() => onDraftChange(saved)}>
            Discard
          </button>
        ) : null}
        {!sameTeam(draft, sdlcTeam()) ? (
          <button type="button" disabled={busy} onClick={() => onDraftChange(sdlcTeam())}>
            Reset to the SDLC team
          </button>
        ) : null}
      </div>
    </>
  );
}

interface RaciMatrixEditorProps {
  team: TeamDefinition;
  /** The saved team, to mark what changed; omitted for a new room. */
  saved?: TeamDefinition;
  onChange: (team: TeamDefinition) => void;
}

/** Every task type against every role, the PO included, with the task types themselves editable. */
export function RaciMatrixEditor({ team, saved, onChange }: RaciMatrixEditorProps) {
  const [newTaskType, setNewTaskType] = useState("");
  const participants = raciParticipants(team);
  const addNewTaskType = () => {
    if (!newTaskType.trim()) return;
    onChange(addTaskType(team, newTaskType).team);
    setNewTaskType("");
  };
  return (
    <>
      <div className="raci-matrix-scroll">
        <table className="raci-matrix">
          <thead>
            <tr>
              <th scope="col">Task type</th>
              <th scope="col">Gate</th>
              {participants.map((role) => (
                <th key={role.id} scope="col" className={role.human ? "raci-human" : undefined}>
                  {role.title}
                </th>
              ))}
              <th scope="col">
                <span className="visually-hidden">Remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {team.taskTypes.map((row) => {
              const base = saved ? taskTypeOf(saved, row.id) : undefined;
              return (
                <tr key={row.id}>
                  <th scope="row">
                    <input
                      className={`task-type-title${saved && base?.title !== row.title ? " raci-changed-input" : ""}`}
                      aria-label={`Task type ${row.title}`}
                      value={row.title}
                      maxLength={80}
                      onChange={(event) => onChange(updateTaskType(team, row.id, { title: event.target.value }))}
                    />
                  </th>
                  <td>
                    <select
                      aria-label={`Gate for ${row.title}`}
                      value={row.gate ?? ""}
                      onChange={(event) => onChange(updateTaskType(team, row.id, { gate: (event.target.value || null) as GateType | null }))}
                    >
                      <option value="">None</option>
                      {(Object.keys(GATE_TITLES) as GateType[]).map((gate) => (
                        <option key={gate} value={gate}>
                          {GATE_TITLES[gate]}
                        </option>
                      ))}
                    </select>
                  </td>
                  {participants.map((role) => {
                    const letter = row.assignments[role.id] ?? "I";
                    const changed = saved !== undefined && letter !== (base?.assignments[role.id] ?? null);
                    return (
                      <td key={role.id} className={`raci-cell raci-cell-${letter}${changed ? " raci-changed" : ""}`}>
                        <select
                          aria-label={`${role.title} for ${row.title}`}
                          title={`${role.title}: ${RACI_LABELS[letter]}`}
                          value={letter}
                          onChange={(event) => onChange(assign(team, row.id, role.id, event.target.value as Raci))}
                        >
                          {RACI_LETTERS.map((option) => (
                            <option key={option} value={option}>
                              {LETTER_NAMES[option]}
                            </option>
                          ))}
                        </select>
                      </td>
                    );
                  })}
                  <td>
                    <button type="button" className="row-remove" aria-label={`Remove task type ${row.title}`} onClick={() => onChange(removeTaskType(team, row.id))}>
                      ×
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="add-task-type" role="group" aria-label="Add a task type">
        <input
          aria-label="New task type"
          placeholder="New task type, e.g. Write user docs"
          maxLength={80}
          value={newTaskType}
          onChange={(event) => setNewTaskType(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              addNewTaskType();
            }
          }}
        />
        <button type="button" disabled={!newTaskType.trim()} onClick={addNewTaskType}>
          + Add task type
        </button>
      </div>
      <p className="muted small raci-legend">
        {RACI_LETTERS.map((letter) => `${LETTER_NAMES[letter]} ${RACI_LABELS[letter].toLowerCase()}`).join(" · ")}. One role is accountable per task type;
        making another role accountable moves it. A new task type starts with the PM responsible and accountable.
      </p>
    </>
  );
}

interface RoleRaciEditorProps {
  team: TeamDefinition;
  saved?: TeamDefinition;
  role: string;
  onChange: (team: TeamDefinition) => void;
}

/** One role's letter for every task type, with who else is accountable and responsible. */
export function RoleRaciEditor({ team, saved, role, onChange }: RoleRaciEditorProps) {
  const participants = raciParticipants(team);
  const holders = (assignments: Record<string, Raci>, test: (letter: Raci) => boolean) =>
    participants.filter((entry) => test(assignments[entry.id] ?? "I")).map((entry) => entry.title).join(", ") || "—";
  const title = participants.find((entry) => entry.id === role)?.title ?? role;
  return (
    <table className="raci-table">
      <thead>
        <tr>
          <th scope="col">Task type</th>
          <th scope="col">{title}</th>
          <th scope="col">Accountable</th>
          <th scope="col">Responsible</th>
        </tr>
      </thead>
      <tbody>
        {team.taskTypes.map((row) => {
          const letter = row.assignments[role] ?? "I";
          const changed = saved !== undefined && letter !== (taskTypeOf(saved, row.id)?.assignments[role] ?? null);
          return (
            <tr key={row.id} className={changed ? "raci-changed" : undefined}>
              <th scope="row">
                {row.title}
                {row.gate ? <span className="muted small"> · {GATE_TITLES[row.gate]} gate</span> : null}
              </th>
              <td>
                <select aria-label={`${title} for ${row.title}`} value={letter} onChange={(event) => onChange(assign(team, row.id, role, event.target.value as Raci))}>
                  {RACI_LETTERS.map((option) => (
                    <option key={option} value={option}>
                      {LETTER_NAMES[option]} · {RACI_LABELS[option]}
                    </option>
                  ))}
                </select>
              </td>
              <td className="small">{holders(row.assignments, (value) => value.includes("A"))}</td>
              <td className="small">{holders(row.assignments, (value) => value.includes("R"))}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

interface RoleDefinitionEditorProps {
  team: TeamDefinition;
  role: TeamRole;
  hostId: string | undefined;
  onChange: (team: TeamDefinition) => void;
  /** Called after the role is taken off the draft team. */
  onRemoved: () => void;
}

/** A role's title (custom roles), brief, and default harness, and taking it off the team. */
export function RoleDefinitionEditor({ team, role, hostId, onChange, onRemoved }: RoleDefinitionEditorProps) {
  return (
    <div className="role-definition">
      {role.builtIn ? (
        <p className="muted small">An SDLC role; its title stays {role.title}. Edit its brief to change what it does.</p>
      ) : (
        <label>
          Title{" "}
          <input value={role.title} maxLength={40} onChange={(event) => onChange(updateRole(team, role.id, { title: event.target.value }))} />
        </label>
      )}
      <label>
        Brief <span className="muted small">(opens the role's prompt: what it does and how it works with the others)</span>
        <textarea rows={5} maxLength={4000} value={role.brief} onChange={(event) => onChange(updateRole(team, role.id, { brief: event.target.value }))} />
      </label>
      <label className="inline-field">
        Default harness{" "}
        <HarnessSelect hostId={hostId} value={role.defaultHarness} onChange={(harness) => harness && onChange(updateRole(team, role.id, { defaultHarness: harness }))} />
      </label>
      {role.id !== "pm" ? (
        <div className="team-config-actions">
          <button
            type="button"
            className="danger"
            onClick={() => {
              onChange(removeRole(team, role.id));
              onRemoved();
            }}
          >
            Remove {role.title} from the team
          </button>
        </div>
      ) : (
        <p className="muted small">The {role.title} coordinates the room, so it stays on every team.</p>
      )}
    </div>
  );
}

/** Every role's open tasks side by side, the PO included. */
export function TasksByRole({ view, team }: { view: BranchView; team: TeamDefinition }) {
  const participants = raciParticipants(team);
  const openTotal = participants.reduce((sum, role) => sum + tasksFor(view, team, role.id).open.length, 0);
  return (
    <>
      <h4>
        Tasks by role <span className="muted small">· {openTotal} open part{openTotal === 1 ? "" : "s"} across the team</span>
      </h4>
      <div className="tasks-by-role">
        {participants.map((role) => {
          const { open, done } = tasksFor(view, team, role.id);
          return (
            <section key={role.id} className="tasks-by-role-column" aria-label={`${role.title} tasks`}>
              <header>
                <PersonSprite palette={role.id === PO_ROLE ? HUMAN_PALETTE : workerPalette(roleAgentName(role.id))} pixel={2} />
                <strong>{role.human ? `${role.title} (you)` : role.title}</strong>
                <span className="muted small">
                  {open.length} open{done ? ` · ${done} done` : ""}
                </span>
              </header>
              {open.length ? (
                <ul className="role-task-list">
                  {open.map(({ task, part }) => (
                    <TaskLine key={task.id} task={task} part={part} />
                  ))}
                </ul>
              ) : (
                <p className="muted small">Nothing open.</p>
              )}
            </section>
          );
        })}
      </div>
    </>
  );
}

/** One role's open tasks and how many are done. */
export function RoleTasks({ view, team, role }: { view: BranchView; team: TeamDefinition; role: string }) {
  const { open, done } = tasksFor(view, team, role);
  if (open.length === 0) return <p className="muted small">No open tasks{done ? ` · ${done} done` : ""}.</p>;
  return (
    <ul className="role-task-list">
      {open.map(({ task, part }) => (
        <TaskLine key={task.id} task={task} part={part} />
      ))}
      {done ? <li className="muted small">{done} done</li> : null}
    </ul>
  );
}
