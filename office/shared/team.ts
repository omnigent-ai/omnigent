// A room's team: its roles (the SDLC theme's, minus any the PO dropped, plus any the PO
// added) and its task types, each with a RACI and an optional human gate.

import {
  BUILT_IN_AGENT_ROLES,
  COORDINATOR_ROLE,
  DEFAULT_ROLE_HARNESSES,
  type GateType,
  GATE_TITLES,
  type Raci,
  ROLE_BRIEFS,
  ROLES,
  SDLC_MATRIX,
  isBuiltInAgentRole,
  registerRoleTitles,
  roleTitle,
} from "./sdlcTheme.ts";

/** The human Product Owner; on every team, never an agent. */
export const PO_ROLE = "po";

export const RACI_LETTERS: readonly Raci[] = ["R", "A", "RA", "C", "I"];

export const RACI_LABELS: Record<Raci, string> = {
  R: "Responsible",
  A: "Accountable",
  RA: "Responsible and accountable",
  C: "Consulted",
  I: "Informed",
};

export interface TeamRole {
  id: string;
  title: string;
  /** What the role does; it opens the role's prompt. */
  brief: string;
  /** The harness the role runs on unless the PO picks another. */
  defaultHarness: string;
  /** One of the SDLC theme's roles; its title stays the theme's. */
  builtIn: boolean;
}

export interface TeamTaskType {
  id: string;
  title: string;
  /** The human gate tasks of this type wait on, when the room has that gate on. */
  gate: GateType | null;
  /** RACI letter by role id, the PO included. */
  assignments: Record<string, Raci>;
}

export interface TeamDefinition {
  /** Agent roles; the PM is always first. */
  roles: TeamRole[];
  taskTypes: TeamTaskType[];
}

/** Legacy per-room RACI changes over the SDLC matrix, from before teams were editable. */
export type RaciOverrides = Record<string, Record<string, Raci>>;

export const MAX_TEAM_ROLES = 12;
export const MAX_TASK_TYPES = 40;
const MAX_BRIEF = 4_000;
const ID_PATTERN = /^[a-z][a-z0-9_]{0,31}$/;
const RESERVED_IDS = new Set([PO_ROLE, "office", "you"]);
const DEFAULT_CUSTOM_HARNESS = "claude-sdk";

export function sdlcTeam(): TeamDefinition {
  return {
    roles: BUILT_IN_AGENT_ROLES.map((role) => ({
      id: role,
      title: ROLES.find((entry) => entry.id === role)?.title ?? role,
      brief: ROLE_BRIEFS[role],
      defaultHarness: DEFAULT_ROLE_HARNESSES[role],
      builtIn: true,
    })),
    taskTypes: SDLC_MATRIX.map((row) => ({ id: row.id, title: row.title, gate: row.gate, assignments: { ...row.assignments } })),
  };
}

/** Native harnesses: a vendor's own CLI running in a terminal, rather than through an SDK. */
export const NATIVE_HARNESSES = [
  "claude-native",
  "codex-native",
  "antigravity-native",
  "cursor-native",
  "pi-native",
  "opencode-native",
  "goose-native",
  "hermes-native",
  "kimi-native",
  "kiro-native",
  "qwen-native",
  "devin-native",
] as const;

export const DEFAULT_NATIVE_HARNESS = "claude-native";

export function isNativeHarness(harness: string): boolean {
  return /-native$|^native-/.test(harness);
}

export const NATIVE_AGENT_TITLE = "Full Stack Developer (FSD)";

const NATIVE_AGENT_BRIEF = [
  `You are the ${NATIVE_AGENT_TITLE}, the only agent in this project room, running in your own native CLI.`,
  "The human is the Product Owner (PO).",
  "There is no one to dispatch to: plan the work as tasks with office_task_create, then do each task yourself,",
  "reporting progress with office_task_update and finishing with office_task_complete. Keep the board current.",
  "You may draft requirements with office_board_propose, but only the PO accepts them. For an interview, list your",
  "questions with office_interview_plan and ask exactly one at a time with office_interview_ask.",
  "Request a human gate with office_gate_request where a task needs one, and never claim a gate is approved",
  "until the office reports it.",
].join(" ");

/**
 * The native theme's team: one Full Stack Developer on a native harness, responsible for every
 * task type. It takes the PM's place, since the room's coordinator is always the PM role.
 * The PO keeps its SDLC letters, so it still owns the scope.
 */
export function nativeTeam(): TeamDefinition {
  const sdlc = sdlcTeam();
  const coordinator = sdlc.roles.find((role) => role.id === COORDINATOR_ROLE) as TeamRole;
  return {
    roles: [{ ...coordinator, title: NATIVE_AGENT_TITLE, brief: NATIVE_AGENT_BRIEF, defaultHarness: DEFAULT_NATIVE_HARNESS }],
    taskTypes: sdlc.taskTypes.map((taskType) => {
      const owner = taskType.assignments[PO_ROLE] ?? "I";
      const agent: Raci = owner === "RA" ? "C" : owner === "A" ? "R" : "RA";
      return { ...taskType, assignments: { [PO_ROLE]: owner, [COORDINATOR_ROLE]: agent } };
    }),
  };
}

/** A room's team: its own definition, or the SDLC team with any legacy RACI changes. */
export function teamOf(runtime: { team?: TeamDefinition; raci?: RaciOverrides } | undefined): TeamDefinition {
  if (runtime?.team) return runtime.team;
  const team = sdlcTeam();
  for (const taskType of team.taskTypes) Object.assign(taskType.assignments, runtime?.raci?.[taskType.id]);
  return team;
}

/** Remember a team's role titles so any view can name its roles. */
export function registerTeam(team: TeamDefinition): void {
  registerRoleTitles(team.roles);
}

export function teamRoleIds(team: TeamDefinition): string[] {
  return team.roles.map((role) => role.id);
}

export function hasRole(team: TeamDefinition, value: unknown): value is string {
  return typeof value === "string" && team.roles.some((role) => role.id === value);
}

export function teamRole(team: TeamDefinition, id: string): TeamRole | undefined {
  return team.roles.find((role) => role.id === id);
}

/** The short form of a role title: "FSD" for "Full Stack Developer (FSD)", otherwise the title. */
export function shortRoleTitle(title: string): string {
  return /\(([A-Za-z0-9]+)\)\s*$/.exec(title)?.[1] ?? title;
}

/** The open room's coordinator, short: "PM", or "FSD" on the native team. */
export function coordinatorName(): string {
  return shortRoleTitle(roleTitle(COORDINATOR_ROLE));
}

/** A role's title on this team, which may rename one of the theme's roles; the PO is "PO". */
export function teamRoleTitle(team: TeamDefinition, id: string): string {
  return teamRole(team, id)?.title ?? roleTitle(id);
}

/** Everyone a RACI can name: the PM, the PO, then the other roles. */
export function raciParticipants(team: TeamDefinition): { id: string; title: string; human: boolean }[] {
  const [first, ...rest] = team.roles;
  return [
    ...(first ? [{ id: first.id, title: first.title, human: false }] : []),
    { id: PO_ROLE, title: "PO", human: true },
    ...rest.map((role) => ({ id: role.id, title: role.title, human: false })),
  ];
}

export function taskTypeOf(team: TeamDefinition, id: string): TeamTaskType | undefined {
  return team.taskTypes.find((entry) => entry.id === id);
}

export function rolesWithLetter(taskType: TeamTaskType, test: (letter: Raci) => boolean): string[] {
  return Object.entries(taskType.assignments)
    .filter(([, letter]) => test(letter))
    .map(([role]) => role);
}

export const responsibleOf = (taskType: TeamTaskType) => rolesWithLetter(taskType, (letter) => letter.includes("R"));
export const consultedOf = (taskType: TeamTaskType) => rolesWithLetter(taskType, (letter) => letter === "C");
export const accountableOf = (taskType: TeamTaskType) => rolesWithLetter(taskType, (letter) => letter.includes("A"))[0];

/** Why the team cannot run, if anything; the office refuses a team with problems. */
export function teamProblems(team: TeamDefinition): string[] {
  const problems: string[] = [];
  if (team.roles[0]?.id !== COORDINATOR_ROLE) problems.push("The PM must be on the team; it coordinates the room.");
  if (team.roles.length > MAX_TEAM_ROLES) problems.push(`A team can have at most ${MAX_TEAM_ROLES} roles.`);
  const titles = new Set<string>();
  for (const role of team.roles) {
    if (!ID_PATTERN.test(role.id) || RESERVED_IDS.has(role.id)) problems.push(`"${role.title}" has an unusable id.`);
    const title = role.title.trim().toLowerCase();
    if (!title) problems.push("Every role needs a title.");
    else if (title === "po" || titles.has(title)) problems.push(`Two roles are called "${role.title}".`);
    titles.add(title);
    if (!role.brief.trim()) problems.push(`${role.title} needs a brief saying what it does.`);
  }
  if (team.taskTypes.length === 0) problems.push("The team needs at least one task type.");
  if (team.taskTypes.length > MAX_TASK_TYPES) problems.push(`A team can have at most ${MAX_TASK_TYPES} task types.`);
  const taskTitles = new Set<string>();
  for (const taskType of team.taskTypes) {
    const title = taskType.title.trim().toLowerCase();
    if (!title) problems.push("Every task type needs a title.");
    else if (taskTitles.has(title)) problems.push(`Two task types are called "${taskType.title}".`);
    taskTitles.add(title);
    const accountable = rolesWithLetter(taskType, (letter) => letter.includes("A")).length;
    if (accountable !== 1) problems.push(`${taskType.title} needs exactly one accountable role (has ${accountable}).`);
    if (responsibleOf(taskType).length === 0) problems.push(`${taskType.title} needs at least one responsible role.`);
  }
  return problems;
}

function slug(title: string): string {
  const base = title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 28);
  return /^[a-z]/.test(base) ? base : `r_${base || "role"}`;
}

function uniqueId(title: string, taken: Iterable<string>): string {
  const used = new Set([...taken, ...RESERVED_IDS, ...BUILT_IN_AGENT_ROLES]);
  const base = slug(title);
  let id = base;
  for (let suffix = 2; used.has(id); suffix++) id = `${base}_${suffix}`;
  return id;
}

/** Fill every task type's RACI for every role (missing roles are informed) and drop unknown roles. */
function withCompleteAssignments(team: TeamDefinition): TeamDefinition {
  const ids = [PO_ROLE, ...teamRoleIds(team)];
  return {
    ...team,
    taskTypes: team.taskTypes.map((taskType) => ({
      ...taskType,
      assignments: Object.fromEntries(ids.map((id) => [id, taskType.assignments[id] ?? "I"])) as Record<string, Raci>,
    })),
  };
}

/**
 * A team from untrusted input, normalized: built-in roles keep the theme's titles, every
 * RACI covers every role, and unknown fields are dropped. Returns its problems instead when
 * it cannot run.
 */
export function parseTeam(input: unknown): { team: TeamDefinition } | { error: string } {
  const record = (input && typeof input === "object" ? input : {}) as Record<string, unknown>;
  const text = (value: unknown, max: number) => (typeof value === "string" ? value.trim().slice(0, max) : "");
  const roles: TeamRole[] = (Array.isArray(record.roles) ? record.roles : []).map((entry) => {
    const role = (entry && typeof entry === "object" ? entry : {}) as Record<string, unknown>;
    const id = text(role.id, 40);
    const builtIn = isBuiltInAgentRole(id);
    return {
      id,
      // A team may rename a theme role, as the native theme does its one agent.
      title: text(role.title, 40) || (builtIn ? (ROLES.find((definition) => definition.id === id)?.title ?? id) : ""),
      brief: text(role.brief, MAX_BRIEF) || (builtIn ? ROLE_BRIEFS[id] : ""),
      defaultHarness: text(role.defaultHarness, 100) || (builtIn ? DEFAULT_ROLE_HARNESSES[id] : DEFAULT_CUSTOM_HARNESS),
      builtIn,
    };
  });
  const gates = Object.keys(GATE_TITLES);
  const taskTypes: TeamTaskType[] = (Array.isArray(record.taskTypes) ? record.taskTypes : []).map((entry) => {
    const taskType = (entry && typeof entry === "object" ? entry : {}) as Record<string, unknown>;
    const assignments: Record<string, Raci> = {};
    if (taskType.assignments && typeof taskType.assignments === "object") {
      for (const [role, letter] of Object.entries(taskType.assignments as Record<string, unknown>)) {
        if ((RACI_LETTERS as readonly unknown[]).includes(letter)) assignments[role] = letter as Raci;
      }
    }
    return {
      id: text(taskType.id, 40),
      title: text(taskType.title, 80),
      gate: gates.includes(taskType.gate as string) ? (taskType.gate as GateType) : null,
      assignments,
    };
  });
  const ids = new Set<string>();
  for (const item of [...roles, ...taskTypes]) {
    if (!ID_PATTERN.test(item.id) || ids.has(`${"brief" in item ? "role" : "task"}:${item.id}`)) {
      return { error: `"${item.title || item.id}" has a missing or repeated id.` };
    }
    ids.add(`${"brief" in item ? "role" : "task"}:${item.id}`);
  }
  const team = withCompleteAssignments({ roles, taskTypes });
  const problems = teamProblems(team);
  return problems.length ? { error: problems.join(" ") } : { team };
}

/** Teams are equal when their roles, task types, and RACI are. */
export function sameTeam(a: TeamDefinition, b: TeamDefinition): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

// Editing helpers for the Team configuration and New room screens. Each returns a new team.

export function addRole(team: TeamDefinition, title: string): { team: TeamDefinition; roleId: string } {
  const roleId = uniqueId(title, teamRoleIds(team));
  const role: TeamRole = {
    id: roleId,
    title: title.trim(),
    brief: `You are the ${title.trim()} on this team. Describe what this role is responsible for and how it works with the others.`,
    defaultHarness: DEFAULT_CUSTOM_HARNESS,
    builtIn: false,
  };
  return { team: withCompleteAssignments({ ...team, roles: [...team.roles, role] }), roleId };
}

/** Add one of the theme's roles back to a team that dropped it. */
export function addBuiltInRole(team: TeamDefinition, roleId: string): TeamDefinition {
  const builtIn = sdlcTeam().roles.find((role) => role.id === roleId);
  if (!builtIn || hasRole(team, roleId)) return team;
  return withCompleteAssignments({ ...team, roles: [...team.roles, builtIn] });
}

/** Remove a role and its RACI letters; the PM stays. Task types it alone was R or A for then show as problems. */
export function removeRole(team: TeamDefinition, roleId: string): TeamDefinition {
  if (roleId === COORDINATOR_ROLE) return team;
  return {
    roles: team.roles.filter((role) => role.id !== roleId),
    taskTypes: team.taskTypes.map((taskType) => {
      const { [roleId]: _removed, ...assignments } = taskType.assignments;
      return { ...taskType, assignments };
    }),
  };
}

export function updateRole(team: TeamDefinition, roleId: string, patch: Partial<Pick<TeamRole, "title" | "brief" | "defaultHarness">>): TeamDefinition {
  return {
    ...team,
    roles: team.roles.map((role) =>
      role.id !== roleId ? role : { ...role, ...patch, title: role.builtIn ? role.title : (patch.title ?? role.title) },
    ),
  };
}

export function addTaskType(team: TeamDefinition, title: string): { team: TeamDefinition; taskTypeId: string } {
  const taskTypeId = uniqueId(title, team.taskTypes.map((entry) => entry.id));
  const taskType: TeamTaskType = { id: taskTypeId, title: title.trim(), gate: null, assignments: { [COORDINATOR_ROLE]: "RA" } };
  return { team: withCompleteAssignments({ ...team, taskTypes: [...team.taskTypes, taskType] }), taskTypeId };
}

export function removeTaskType(team: TeamDefinition, taskTypeId: string): TeamDefinition {
  return { ...team, taskTypes: team.taskTypes.filter((entry) => entry.id !== taskTypeId) };
}

export function updateTaskType(team: TeamDefinition, taskTypeId: string, patch: Partial<Pick<TeamTaskType, "title" | "gate">>): TeamDefinition {
  return { ...team, taskTypes: team.taskTypes.map((entry) => (entry.id === taskTypeId ? { ...entry, ...patch } : entry)) };
}

/**
 * Give a role a letter for a task type. A task type has one accountable role, so making a
 * role accountable moves accountability off the previous holder (who stays responsible if it was
 * R+A, and otherwise becomes consulted).
 */
export function assign(team: TeamDefinition, taskTypeId: string, roleId: string, letter: Raci): TeamDefinition {
  return {
    ...team,
    taskTypes: team.taskTypes.map((taskType) => {
      if (taskType.id !== taskTypeId) return taskType;
      const assignments = { ...taskType.assignments };
      if (letter.includes("A")) {
        for (const [other, current] of Object.entries(assignments)) {
          if (other === roleId) continue;
          if (current === "A") assignments[other] = "C";
          else if (current === "RA") assignments[other] = "R";
        }
      }
      assignments[roleId] = letter;
      return { ...taskType, assignments };
    }),
  };
}

/** A short account of what changed between two teams, for the event feed; empty when nothing did. */
export function describeTeamChange(before: TeamDefinition, after: TeamDefinition): string {
  const parts: string[] = [];
  for (const role of after.roles) if (!hasRole(before, role.id)) parts.push(`added ${role.title}`);
  for (const role of before.roles) if (!hasRole(after, role.id)) parts.push(`removed ${role.title}`);
  for (const role of after.roles) {
    const previous = teamRole(before, role.id);
    if (previous && !role.builtIn && previous.title !== role.title) parts.push(`renamed ${previous.title} to ${role.title}`);
    if (previous && previous.brief !== role.brief) parts.push(`rewrote the ${role.title} brief`);
  }
  for (const taskType of after.taskTypes) if (!taskTypeOf(before, taskType.id)) parts.push(`added task type "${taskType.title}"`);
  for (const taskType of before.taskTypes) if (!taskTypeOf(after, taskType.id)) parts.push(`removed task type "${taskType.title}"`);
  let raciChanges = 0;
  for (const taskType of after.taskTypes) {
    const previous = taskTypeOf(before, taskType.id);
    if (!previous) continue;
    if (previous.title !== taskType.title) parts.push(`renamed task type "${previous.title}" to "${taskType.title}"`);
    if (previous.gate !== taskType.gate) parts.push(`${taskType.gate ? `put "${taskType.title}" behind the ${taskType.gate} gate` : `removed the gate from "${taskType.title}"`}`);
    for (const [role, letter] of Object.entries(taskType.assignments)) {
      if (hasRole(before, role) || role === PO_ROLE) {
        if (previous.assignments[role] !== letter) {
          raciChanges++;
          if (raciChanges <= 4) parts.push(`${role === PO_ROLE ? "PO" : (teamRole(after, role)?.title ?? role)} ${RACI_LABELS[letter].toLowerCase()} for ${taskType.title}`);
        }
      }
    }
  }
  if (raciChanges > 4) parts.push(`${raciChanges - 4} more RACI change(s)`);
  return parts.join("; ");
}
