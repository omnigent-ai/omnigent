// Team files: a room's team configuration as YAML, to export, edit by hand, and import as a
// team template. Roles carry their brief and setup; task types carry their RACI by role id.

import { parse, stringify } from "yaml";
import { parseRoleToolSettings } from "./roleTools.ts";
import { type GateType } from "./sdlcTheme.ts";
import { PO_ROLE, type TeamDefinition, parseTeam, teamOf } from "./team.ts";
import { type TeamRoleTemplate, type TeamTemplate, templateRolesFromRuntime, templateTeam } from "./teamTemplates.ts";
import type { BranchRuntime } from "./workflowTypes.ts";

export const TEAM_FILE_KIND = "omnigent-office-team";
export const TEAM_FILE_VERSION = 1;
const MAX_FILE_BYTES = 256 * 1024;

interface TeamFileRole {
  id: string;
  title: string;
  brief: string;
  defaultHarness: string;
  harness?: string;
  model?: string;
  effort?: string;
  instructions?: string;
  tools?: TeamRoleTemplate["toolSettings"];
}

interface TeamFileTaskType {
  id: string;
  title: string;
  gate: GateType | null;
  raci: Record<string, string>;
}

export interface TeamFile {
  kind: typeof TEAM_FILE_KIND;
  version: typeof TEAM_FILE_VERSION;
  name: string;
  description?: string;
  roles: TeamFileRole[];
  taskTypes: TeamFileTaskType[];
}

function fileFromTeam(name: string, description: string | null, team: TeamDefinition, roles: TeamTemplate["roles"]): TeamFile {
  return {
    kind: TEAM_FILE_KIND,
    version: TEAM_FILE_VERSION,
    name,
    ...(description ? { description } : {}),
    roles: team.roles.map((role) => {
      const setup = roles[role.id] ?? {};
      return {
        id: role.id,
        title: role.title,
        brief: role.brief,
        defaultHarness: role.defaultHarness,
        ...(setup.harness ? { harness: setup.harness } : {}),
        ...(setup.model ? { model: setup.model } : {}),
        ...(setup.effort ? { effort: setup.effort } : {}),
        ...(setup.instructions ? { instructions: setup.instructions } : {}),
        ...(setup.toolSettings ? { tools: setup.toolSettings } : {}),
      };
    }),
    // Informed is the default, so only the other letters are written.
    taskTypes: team.taskTypes.map((taskType) => ({
      id: taskType.id,
      title: taskType.title,
      gate: taskType.gate,
      raci: Object.fromEntries(Object.entries(taskType.assignments).filter(([, letter]) => letter !== "I")),
    })),
  };
}

function yamlOf(file: TeamFile): string {
  return `# Omnigent office team. Import it from Team configuration or the New room Team tab.\n${stringify(file, { lineWidth: 100 })}`;
}

/** A room's team, with each role's setup, as a team file. */
export function teamFileYaml(runtime: BranchRuntime, name: string, description: string | null = null): string {
  return yamlOf(fileFromTeam(name, description, teamOf(runtime), templateRolesFromRuntime(runtime)));
}

/** A saved team template as a team file. */
export function templateFileYaml(template: TeamTemplate): string {
  return yamlOf(fileFromTeam(template.name, template.description, templateTeam(template), template.roles));
}

/** A file name for a team file. */
export function teamFileName(name: string): string {
  return `${name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "team"}.team.yaml`;
}

const optionalText = (value: unknown, max: number) => (typeof value === "string" && value.trim() ? value.trim().slice(0, max) : undefined);

/**
 * A team file's name, description, team, and role setups, checked the way the office checks
 * a team. `toolsFor` lists the office tools a role may turn off.
 */
export function parseTeamFile(
  text: string,
  toolsFor: (role: string) => string[],
): { name: string; description: string | null; team: TeamDefinition; roles: TeamTemplate["roles"] } | { error: string } {
  if (text.length > MAX_FILE_BYTES) return { error: "The team file is larger than 256 KB." };
  let document: unknown;
  try {
    document = parse(text);
  } catch (error) {
    return { error: `The team file is not valid YAML: ${(error as Error).message.split("\n")[0]}` };
  }
  const record = (document && typeof document === "object" ? document : {}) as Record<string, unknown>;
  if (record.kind !== TEAM_FILE_KIND) return { error: `This is not a team file (expected kind: ${TEAM_FILE_KIND}).` };
  if (record.version !== TEAM_FILE_VERSION) return { error: `Unsupported team file version ${String(record.version)}; this office reads version ${TEAM_FILE_VERSION}.` };
  const name = optionalText(record.name, 60);
  if (!name) return { error: "The team file needs a name." };
  const fileRoles = (Array.isArray(record.roles) ? record.roles : []) as Record<string, unknown>[];
  const fileTaskTypes = (Array.isArray(record.taskTypes) ? record.taskTypes : []) as Record<string, unknown>[];
  const parsed = parseTeam({
    roles: fileRoles,
    taskTypes: fileTaskTypes.map((entry) => ({ ...(entry && typeof entry === "object" ? entry : {}), assignments: entry?.raci })),
  });
  if ("error" in parsed) return parsed;
  const roles: TeamTemplate["roles"] = {};
  for (const entry of fileRoles) {
    const id = String(entry?.id ?? "").trim();
    if (id === PO_ROLE) continue;
    const setup: TeamRoleTemplate = {};
    const harness = optionalText(entry.harness, 100);
    if (harness) setup.harness = harness;
    const model = optionalText(entry.model, 200);
    if (model) setup.model = model;
    const effort = optionalText(entry.effort, 40);
    if (effort) setup.effort = effort;
    const instructions = optionalText(entry.instructions, 8000);
    if (instructions) setup.instructions = instructions;
    if (entry.tools !== undefined && entry.tools !== null) {
      const tools = parseRoleToolSettings(entry.tools, id, toolsFor(id));
      if ("error" in tools) return { error: `${String(entry.title ?? id)}: ${tools.error}` };
      if (Object.keys(tools.settings).length) setup.toolSettings = tools.settings;
    }
    if (Object.keys(setup).length) roles[id] = setup;
  }
  return { name, description: optionalText(record.description, 300) ?? null, team: parsed.team, roles };
}
