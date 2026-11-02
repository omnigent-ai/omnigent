// Team templates: a named, reusable team (its roles, task types, and RACI) with each role's
// harness, model, effort, instructions, tools, and policies, which a room can switch to.

import type { RoleToolSettings } from "./roleTools.ts";
import { type RaciOverrides, type TeamDefinition, nativeTeam, sameTeam, sdlcTeam, teamOf, teamRole } from "./team.ts";
import type { AgentRoleId } from "./sdlcTheme.ts";
import type { BranchRuntime } from "./workflowTypes.ts";

export interface TeamRoleTemplate {
  harness?: string;
  model?: string;
  effort?: string;
  instructions?: string;
  toolSettings?: Omit<RoleToolSettings, "updatedAt">;
}

export interface TeamTemplate {
  id: string;
  name: string;
  description: string | null;
  createdAt: number;
  updatedAt: number;
  /** Shipped with the office; cannot be replaced or deleted. */
  builtIn?: boolean;
  /** The team's roles and task types; absent means the SDLC team. */
  team?: TeamDefinition;
  /** Only the roles whose setup differs from their defaults. */
  roles: Partial<Record<AgentRoleId, TeamRoleTemplate>>;
  /** RACI changes saved by templates from before teams were editable. */
  raci?: RaciOverrides;
}

export const DEFAULT_TEAM_TEMPLATE: TeamTemplate = {
  id: "builtin_default",
  name: "Default team",
  description: "The SDLC team: every role on its default harness and model, with no extra instructions, tools, or policies.",
  createdAt: 0,
  updatedAt: 0,
  builtIn: true,
  roles: {},
};

/** The native theme: one agent, the PM, doing every task itself in a native CLI. */
export const NATIVE_TEAM_TEMPLATE: TeamTemplate = {
  id: "builtin_native",
  name: "Native (single agent)",
  description: "One Full Stack Developer (FSD) on a native CLI harness such as claude-native, planning and doing every task itself.",
  createdAt: 0,
  updatedAt: 0,
  builtIn: true,
  team: nativeTeam(),
  roles: {},
};

export const BUILT_IN_TEAM_TEMPLATES: readonly TeamTemplate[] = [DEFAULT_TEAM_TEMPLATE, NATIVE_TEAM_TEMPLATE];

export type TemplateField = "added" | "removed" | "harness" | "model" | "effort" | "instructions" | "tools" | "raci";

export const TEMPLATE_FIELD_LABELS: Record<TemplateField, string> = {
  added: "joins the team",
  removed: "leaves the team",
  harness: "harness",
  model: "model",
  effort: "effort",
  instructions: "instructions",
  tools: "tools and policies",
  raci: "RACI",
};

export interface TemplateDiff {
  roles: { role: AgentRoleId; title: string; fields: TemplateField[] }[];
  /** Task types added, removed, or renamed, for the PO. */
  taskTypes: string[];
}

/** The template's team. */
export function templateTeam(template: TeamTemplate): TeamDefinition {
  return template.team ?? teamOf({ raci: template.raci });
}

/** The harness a role runs on: its own choice, the room-wide override, or its team default. */
export function effectiveRoleHarness(runtime: BranchRuntime, role: AgentRoleId): string {
  return runtime.roleHarnesses?.[role] ?? runtime.harness ?? teamRole(teamOf(runtime), role)?.defaultHarness ?? "claude-sdk";
}

function toolKey(settings: RoleToolSettings | undefined): string {
  return JSON.stringify({
    disabled: [...(settings?.disabledTools ?? [])].sort(),
    servers: settings?.mcpServers ?? [],
    risk: settings?.riskPolicy ?? "ask",
  });
}

/** A role's setup as a template entry; empty when it matches the role's defaults. */
function roleFromRuntime(runtime: BranchRuntime, role: AgentRoleId): TeamRoleTemplate {
  const entry: TeamRoleTemplate = {};
  const harness = effectiveRoleHarness(runtime, role);
  if (harness !== teamRole(teamOf(runtime), role)?.defaultHarness) entry.harness = harness;
  const model = runtime.roleModels?.[role] ?? runtime.model;
  if (model) entry.model = model;
  const effort = runtime.roleEfforts?.[role];
  if (effort) entry.effort = effort;
  const instructions = runtime.roleCustomInstructions?.[role];
  if (instructions) entry.instructions = instructions;
  const tools = runtime.roleToolSettings?.[role];
  if (toolKey(tools) !== toolKey(undefined)) {
    const { updatedAt: _updatedAt, ...rest } = tools as RoleToolSettings;
    entry.toolSettings = rest;
  }
  return entry;
}

/** The current team of a room, as template roles. */
export function templateRolesFromRuntime(runtime: BranchRuntime): TeamTemplate["roles"] {
  const roles: TeamTemplate["roles"] = {};
  for (const role of teamOf(runtime).roles) {
    const entry = roleFromRuntime(runtime, role.id);
    if (Object.keys(entry).length) roles[role.id] = entry;
  }
  return roles;
}

function raciOfRole(team: TeamDefinition, role: string): string {
  return team.taskTypes.map((taskType) => `${taskType.id}:${taskType.assignments[role] ?? "-"}`).join();
}

/** What switching to the template would change: roles joining, leaving, or set up differently, and task types. */
export function templateDiff(runtime: BranchRuntime, template: TeamTemplate): TemplateDiff {
  const current = teamOf(runtime);
  const next = templateTeam(template);
  const ids = [...new Set([...current.roles.map((role) => role.id), ...next.roles.map((role) => role.id)])];
  const roles = ids
    .map((role) => {
      const before = teamRole(current, role);
      const after = teamRole(next, role);
      const title = (after ?? before)?.title ?? role;
      if (!before) return { role, title, fields: ["added"] as TemplateField[] };
      if (!after) return { role, title, fields: ["removed"] as TemplateField[] };
      const now = roleFromRuntime(runtime, role);
      const then = template.roles[role] ?? {};
      const fields: TemplateField[] = [];
      if ((now.harness ?? before.defaultHarness) !== (then.harness ?? after.defaultHarness)) fields.push("harness");
      if ((now.model ?? "") !== (then.model ?? "")) fields.push("model");
      if ((now.effort ?? "") !== (then.effort ?? "")) fields.push("effort");
      if ((now.instructions ?? "") !== (then.instructions ?? "") || before.brief !== after.brief) fields.push("instructions");
      if (toolKey(now.toolSettings) !== toolKey(then.toolSettings)) fields.push("tools");
      if (raciOfRole(current, role) !== raciOfRole(next, role)) fields.push("raci");
      return { role, title, fields };
    })
    .filter((change) => change.fields.length > 0);
  const taskTypes = [
    ...next.taskTypes.filter((entry) => !current.taskTypes.some((other) => other.id === entry.id)).map((entry) => `adds "${entry.title}"`),
    ...current.taskTypes.filter((entry) => !next.taskTypes.some((other) => other.id === entry.id)).map((entry) => `removes "${entry.title}"`),
    ...next.taskTypes.flatMap((entry) => {
      const before = current.taskTypes.find((other) => other.id === entry.id);
      return before && (before.title !== entry.title || before.gate !== entry.gate) ? [`changes "${entry.title}"`] : [];
    }),
  ];
  return { roles, taskTypes };
}

/**
 * The room's runtime with the template's team in place of its own. Host, folder, and runner
 * stay; a role whose tools change is marked so its running session reads as out of date.
 */
export function runtimeWithTemplate(runtime: BranchRuntime, template: TeamTemplate, now: number): BranchRuntime {
  const diff = templateDiff(runtime, template);
  const toolsChanged = new Set(diff.roles.filter((change) => change.fields.includes("tools")).map((change) => change.role));
  const team = templateTeam(template);
  const next: BranchRuntime = { ...runtime };
  // Room-wide overrides would leak into roles the template leaves at their defaults.
  delete next.harness;
  delete next.model;
  delete next.raci;
  if (sameTeam(team, sdlcTeam())) delete next.team;
  else next.team = team;
  const pick = <T,>(read: (entry: TeamRoleTemplate) => T | undefined) =>
    Object.fromEntries(
      team.roles.flatMap((role) => {
        const entry = template.roles[role.id];
        const value = entry ? read(entry) : undefined;
        return value === undefined ? [] : [[role.id, value]];
      }),
    ) as Partial<Record<AgentRoleId, T>>;
  next.roleHarnesses = pick((entry) => entry.harness);
  next.roleModels = pick((entry) => entry.model);
  next.roleEfforts = pick((entry) => entry.effort);
  next.roleCustomInstructions = pick((entry) => entry.instructions);
  next.roleToolSettings = Object.fromEntries(
    team.roles.flatMap((role) => {
      const tools = template.roles[role.id]?.toolSettings;
      if (!tools && !toolsChanged.has(role.id)) return [];
      return [[role.id, { ...(tools ?? {}), updatedAt: toolsChanged.has(role.id) ? now : runtime.roleToolSettings?.[role.id]?.updatedAt }]];
    }),
  );
  next.teamTemplateId = template.id;
  return next;
}
