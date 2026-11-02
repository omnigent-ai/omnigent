// Durable SDLC workflow orchestration. The office service owns the task
// graph, requirements board, and workflow gates; Omnigent owns sessions,
// execution, and runtime policy. Role sessions reach this service through
// the office MCP endpoint, and the PO through the office HTTP API.

import { createHash, randomBytes } from "node:crypto";
import {
  COORDINATOR_ROLE,
  DEFAULT_GATES,
  GATE_INVALIDATES,
  GATE_TITLES,
  RUNTIME_GATE_PATTERNS,
  SDLC_MATRIX,
  type AgentRoleId,
  type GateType,
  type RoleId,
} from "../shared/sdlcTheme.ts";
import type {
  BoardProposal,
  BranchRuntime,
  BranchView,
  GateArtifact,
  GateRequest,
  InterviewQuestion,
  InterviewRound,
  RoleStartFailure,
  Requirement,
  RequirementsBoard,
  RoleSessionLink,
  TaskStatus,
  ThemeBranch,
  WorkflowEvent,
  WorkflowState,
  WorkflowTask,
} from "../shared/workflowTypes.ts";
import { EFFORT_ORDER } from "../shared/harnessModels.ts";
import { parseTeamFile } from "../shared/teamFile.ts";
import { MAX_LINKS_PER_PROJECT, type ProjectLink, parseLinkInput } from "../shared/projectLinks.ts";
import {
  PO_ROLE,
  type TeamDefinition,
  accountableOf,
  consultedOf,
  hasRole,
  parseTeam,
  registerTeam,
  teamRoleTitle,
  responsibleOf,
  sameTeam,
  sdlcTeam,
  taskTypeOf,
  describeTeamChange,
  teamOf,
} from "../shared/team.ts";
import { RISK_POLICY_LABELS, parseRoleToolSettings } from "../shared/roleTools.ts";
import {
  BUILT_IN_TEAM_TEMPLATES,
  TEMPLATE_FIELD_LABELS,
  type TeamTemplate,
  runtimeWithTemplate,
  templateDiff,
  templateRolesFromRuntime,
} from "../shared/teamTemplates.ts";
import { gatePolicyExpression, roleAgentSpec, roleBundle, roleHarness, roleInstructions } from "./bundle.ts";
import type { Notifier } from "./notifier.ts";
import type { RoleToolContext } from "./roleTools.ts";
import type { StateStore } from "./stateStore.ts";

export class WorkflowError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** The Omnigent operations the workflow needs; implemented by OmnigentClient. */
export interface WorkflowOmnigent {
  createBundledSession(
    bundle: Buffer,
    metadata: Record<string, unknown>,
  ): Promise<{ session_id: string }>;
  bindRunner(sessionId: string, runnerId: string): Promise<void>;
  sendMessage(sessionId: string, text: string, stableId?: string): Promise<unknown>;
  interrupt(sessionId: string): Promise<unknown>;
  /** Hide a session from Omnigent's lists; it can be unarchived there. */
  archiveSession(sessionId: string): Promise<unknown>;
  addSessionPolicy(sessionId: string, name: string, expression: string): Promise<{ id: string }>;
  deleteSessionPolicy(sessionId: string, policyId: string): Promise<void>;
  listProjects(): Promise<{ id: string; name: string; config?: { host_id?: string; workspace?: string } }[]>;
}

/** Persisted link fields the browser never sees. */
export interface StoredRoleSession extends RoleSessionLink {
  mcpToken: string;
  /** Live gate policy id per runtime gate, plus its version for unique naming. */
  gatePolicies: Partial<Record<GateType, { id: string; version: number }>>;
}

export interface McpCaller {
  link: StoredRoleSession;
  branch: ThemeBranch;
}

export type WorkflowClock = () => number;

const MAX_EVENTS_PER_BRANCH = 500;
const MAX_RELAYED_IDS = 5000;
const ACTIVE_TASK_STATUSES: readonly TaskStatus[] = ["pending", "in_progress", "blocked", "submitted", "awaiting_gate"];

export function newId(prefix: string): string {
  return `${prefix}_${randomBytes(8).toString("hex")}`;
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

export function boardDigest(requirements: Requirement[]): string {
  return sha256(JSON.stringify(requirements.map((entry) => [entry.id, entry.title, entry.description, entry.acceptanceCriteria])));
}

export const THEME_REVISION = sha256(JSON.stringify(SDLC_MATRIX)).slice(0, 16);

export function gatePolicyRevision(gates: Record<GateType, boolean>): string {
  return sha256(JSON.stringify({ gates, patterns: RUNTIME_GATE_PATTERNS })).slice(0, 16);
}

function stringList(value: unknown, field: string, { required = false } = {}): string[] {
  if (value === undefined || value === null) {
    if (required) throw new WorkflowError(400, "invalid_input", `${field} is required`);
    return [];
  }
  if (!Array.isArray(value) || value.some((entry) => typeof entry !== "string")) {
    throw new WorkflowError(400, "invalid_input", `${field} must be a list of strings`);
  }
  return (value as string[]).map((entry) => entry.trim()).filter(Boolean);
}

function text(value: unknown, field: string, { required = true, max = 20_000 } = {}): string {
  if (value === undefined || value === null || value === "") {
    if (required) throw new WorkflowError(400, "invalid_input", `${field} is required`);
    return "";
  }
  if (typeof value !== "string") throw new WorkflowError(400, "invalid_input", `${field} must be a string`);
  if (value.length > max) throw new WorkflowError(400, "invalid_input", `${field} is too long`);
  return value.trim();
}

export function normalizeRequirements(value: unknown): Requirement[] {
  if (!Array.isArray(value)) throw new WorkflowError(400, "invalid_input", "requirements must be a list");
  return value.map((entry, index) => {
    if (!entry || typeof entry !== "object") throw new WorkflowError(400, "invalid_input", `requirement ${index + 1} is invalid`);
    const record = entry as Record<string, unknown>;
    return {
      id: typeof record.id === "string" && record.id ? record.id : newId("req"),
      title: text(record.title, `requirement ${index + 1} title`, { max: 300 }),
      description: text(record.description, `requirement ${index + 1} description`, { required: false }),
      acceptanceCriteria: stringList(record.acceptanceCriteria, `requirement ${index + 1} acceptanceCriteria`),
    };
  });
}

export interface AssignThemeInput {
  runtime: BranchRuntime;
  gates?: Partial<Record<GateType, boolean>>;
}

export class WorkflowService {
  private readonly omnigent: WorkflowOmnigent;
  private readonly store: StateStore;
  private readonly notifier: Notifier;
  private readonly mcpUrl: string;
  private readonly clock: WorkflowClock;

  constructor(options: {
    omnigent: WorkflowOmnigent;
    store: StateStore;
    notifier: Notifier;
    mcpUrl: string;
    clock: WorkflowClock;
  }) {
    this.omnigent = options.omnigent;
    this.store = options.store;
    this.notifier = options.notifier;
    this.mcpUrl = options.mcpUrl;
    this.clock = options.clock;
    this.migrateInterviews();
    for (const branch of Object.values(this.state.branches)) registerTeam(teamOf(branch.runtime));
  }

  /** A role's office tools, set by the HTTP layer that owns the tool list. */
  private toolsFor: ((role: string) => string[]) | null = null;

  setToolCatalog(toolsFor: (role: string) => string[]): void {
    this.toolsFor = toolsFor;
  }

  /** The room's team: its roles, task types, and RACI. */
  team(branchId: string): TeamDefinition {
    return teamOf(this.branch(branchId).runtime);
  }

  /** A role's title on the room's team, which may rename the theme's roles. */
  roleName(branchId: string, role: string): string {
    return teamRoleTitle(teamOf(this.state.branches[branchId]?.runtime), role);
  }

  private requireRole(branchId: string, value: unknown): string {
    if (!hasRole(this.team(branchId), value)) throw new WorkflowError(400, "invalid_role", "That role is not on this room's team.");
    return value;
  }

  /** Questions saved before interview agendas existed become one interview per branch. */
  private migrateInterviews(): void {
    const legacy = Object.values(this.state.interviews).filter((question) => !question.interviewId);
    if (legacy.length === 0) return;
    for (const question of legacy) {
      let round = Object.values(this.state.interviewRounds).find((entry) => entry.branchId === question.branchId && entry.topic === "");
      if (!round) {
        round = { id: newId("interview"), branchId: question.branchId, topic: "", startedAt: question.askedAt ?? 0, endedAt: null };
        this.state.interviewRounds[round.id] = round;
      }
      Object.assign(question, {
        interviewId: round.id,
        status: question.answer === null ? "asked" : "answered",
        origin: "plan",
        addedAt: question.askedAt ?? 0,
        skipReason: null,
      });
    }
    this.save();
  }

  private get state(): WorkflowState {
    return this.store.state.workflow;
  }

  private save(): void {
    this.store.save();
  }

  // ---- Queries ---------------------------------------------------------

  activeBranch(projectId: string): ThemeBranch | null {
    return (
      Object.values(this.state.branches).find(
        (branch) => branch.projectId === projectId && branch.status !== "archived",
      ) ?? null
    );
  }

  branch(branchId: string): ThemeBranch {
    const branch = this.state.branches[branchId];
    if (!branch) throw new WorkflowError(404, "branch_not_found", "No such theme branch");
    return branch;
  }

  private links(branchId: string): StoredRoleSession[] {
    return (this.state.roleSessions as StoredRoleSession[]).filter((link) => link.branchId === branchId);
  }

  private liveLinks(branchId: string): StoredRoleSession[] {
    return this.links(branchId).filter((link) => link.retiredAt === null);
  }

  private persistentLink(branchId: string, role: AgentRoleId): StoredRoleSession | undefined {
    return this.liveLinks(branchId).find((link) => link.role === role && !link.temporary);
  }

  /** Each role's prompt as a new session would get it, and the PO's own instructions for it. */
  async teamConfig(branchId: string): Promise<{ role: AgentRoleId; prompt: string; customInstructions: string }[]> {
    const branch = this.branch(branchId);
    const projectName = await this.projectName(branch.projectId);
    return teamOf(branch.runtime).roles.map(({ id: role }) => ({
      role,
      prompt: roleInstructions({ role, projectName, runtime: branch.runtime }),
      customInstructions: branch.runtime.roleCustomInstructions?.[role] ?? "",
    }));
  }

  /**
   * Replace the room's team: its roles, task types, and RACI. Tasks the PM creates from now on
   * follow it; existing tasks keep their roles. Roles taken off the team stop and retire.
   */
  async setTeam(branchId: string, input: unknown): Promise<BranchView> {
    const branch = this.branch(branchId);
    const parsed = parseTeam(input);
    if ("error" in parsed) throw new WorkflowError(400, "invalid_team", parsed.error);
    const before = teamOf(branch.runtime);
    const summary = describeTeamChange(before, parsed.team);
    this.adoptTeam(branch, parsed.team);
    if (summary) this.recordEvent(branchId, "po", "team_updated", `PO changed the team: ${summary}. New tasks follow it.`);
    this.save();
    await this.retireRemovedRoles(branch, before, parsed.team, "PO took this role off the team.");
    return this.view(branchId);
  }

  /** Store a team on the room, dropping settings of roles that left it. */
  private adoptTeam(branch: ThemeBranch, team: TeamDefinition): void {
    const runtime = branch.runtime;
    delete runtime.raci;
    if (sameTeam(team, sdlcTeam())) delete runtime.team;
    else runtime.team = team;
    for (const field of ["roleHarnesses", "roleModels", "roleEfforts", "roleCustomInstructions", "roleToolSettings"] as const) {
      const values = runtime[field] as Record<string, unknown> | undefined;
      if (!values) continue;
      for (const role of Object.keys(values)) if (!hasRole(team, role)) delete values[role];
    }
    registerTeam(team);
  }

  /** Stop and retire the live sessions of roles a team change removed; tasks keep their history. */
  private async retireRemovedRoles(branch: ThemeBranch, before: TeamDefinition, after: TeamDefinition, reason: string): Promise<void> {
    const removed = before.roles.filter((role) => !hasRole(after, role.id));
    if (!removed.length) return;
    for (const link of this.liveLinks(branch.id).filter((entry) => removed.some((role) => role.id === entry.role))) {
      await this.omnigent.interrupt(link.sessionId).catch(() => undefined);
      link.retiredAt = this.clock();
      this.recordEvent(branch.id, "office", "role_retired", `${this.roleName(branch.id, link.role)} session retired: ${reason}`);
    }
    this.save();
  }

  /** The built-in teams, then saved templates by name. */
  teamTemplates(): TeamTemplate[] {
    const saved = Object.values(this.state.teamTemplates ?? {}).sort((a, b) => a.name.localeCompare(b.name));
    return [...BUILT_IN_TEAM_TEMPLATES, ...saved];
  }

  /** Save the room's team as a template; a template with the same name is replaced. */
  saveTeamTemplate(branchId: string, nameInput: unknown, descriptionInput: unknown): TeamTemplate {
    const branch = this.branch(branchId);
    const name = text(nameInput, "name", { max: 60 });
    const description = text(descriptionInput, "description", { required: false, max: 300 }) || null;
    const builtIn = BUILT_IN_TEAM_TEMPLATES.find((entry) => entry.name.toLowerCase() === name.toLowerCase());
    if (builtIn) throw new WorkflowError(409, "template_builtin", `"${builtIn.name}" is built in; choose another name.`);
    const existing = this.templateNamed(name);
    const template = this.storeTemplate(name, description, teamOf(branch.runtime), templateRolesFromRuntime(branch.runtime));
    branch.runtime.teamTemplateId = template.id;
    this.recordEvent(branchId, "po", "team_template_saved", `PO ${existing ? "updated" : "saved"} the team as the template "${name}".`);
    this.save();
    return template;
  }

  /**
   * Switch the room's team to a template. Changed roles pick it up when they restart;
   * with `restart`, running ones restart now with a handoff.
   */
  async applyTeamTemplate(branchId: string, templateId: string, restart: boolean): Promise<{ view: BranchView; changed: AgentRoleId[] }> {
    const branch = this.branch(branchId);
    const template = this.teamTemplates().find((entry) => entry.id === templateId);
    if (!template) throw new WorkflowError(404, "template_not_found", "That team template no longer exists.");
    const diff = templateDiff(branch.runtime, template);
    const before = teamOf(branch.runtime);
    const next = runtimeWithTemplate(branch.runtime, template, this.clock());
    branch.runtime = next;
    this.adoptTeam(branch, teamOf(next));
    const detail = [
      ...diff.roles.map((change) => `${change.title} (${change.fields.map((field) => TEMPLATE_FIELD_LABELS[field]).join(", ")})`),
      ...diff.taskTypes,
    ].join("; ");
    this.recordEvent(branchId, "po", "team_template_applied", `PO switched the team to "${template.name}"${detail ? `: ${detail}` : "; nothing changed"}.`);
    this.save();
    await this.retireRemovedRoles(branch, before, teamOf(next), `the team switched to "${template.name}".`);
    // Only roles that stay on the team and changed need a fresh session.
    const changed = diff.roles.filter((change) => !change.fields.includes("added") && !change.fields.includes("removed")).map((change) => change.role);
    if (restart) {
      const failures: string[] = [];
      for (const role of changed) {
        if (!this.persistentLink(branchId, role) && role !== COORDINATOR_ROLE) continue;
        await this.rolloverRole(branchId, role, `PO switched the team to "${template.name}".`).catch((error: Error) => failures.push(`${this.roleName(branchId, role)}: ${error.message}`));
      }
      if (failures.length) throw new WorkflowError(502, "template_restart_incomplete", `Template applied, but some roles did not restart. ${failures.join("; ")}`);
    }
    return { view: this.view(branchId), changed };
  }

  /**
   * Save a team file (YAML) as a template, replacing a saved one with its name. A file named
   * after a built-in team is saved as "<name> (imported)".
   */
  importTeamTemplate(yamlInput: unknown): { template: TeamTemplate; replaced: boolean } {
    const parsed = parseTeamFile(text(yamlInput, "yaml", { max: 256 * 1024 }), (role) => this.toolsFor?.(role) ?? []);
    if ("error" in parsed) throw new WorkflowError(400, "invalid_team_file", parsed.error);
    const builtIn = BUILT_IN_TEAM_TEMPLATES.some((entry) => entry.name.toLowerCase() === parsed.name.toLowerCase());
    const name = builtIn ? `${parsed.name.slice(0, 49)} (imported)` : parsed.name;
    const replaced = Boolean(this.templateNamed(name));
    const template = this.storeTemplate(name, parsed.description, parsed.team, parsed.roles);
    this.save();
    return { template, replaced };
  }

  private templateNamed(name: string): TeamTemplate | undefined {
    return Object.values(this.state.teamTemplates ?? {}).find((entry) => entry.name.toLowerCase() === name.toLowerCase());
  }

  private storeTemplate(name: string, description: string | null, team: TeamDefinition, roles: TeamTemplate["roles"]): TeamTemplate {
    this.state.teamTemplates ??= {};
    const existing = this.templateNamed(name);
    const now = this.clock();
    const template: TeamTemplate = {
      id: existing?.id ?? newId("team"),
      name,
      description,
      createdAt: existing?.createdAt ?? now,
      updatedAt: now,
      roles,
      ...(sameTeam(team, sdlcTeam()) ? {} : { team }),
    };
    this.state.teamTemplates[template.id] = template;
    return template;
  }

  deleteTeamTemplate(templateId: string): void {
    if (BUILT_IN_TEAM_TEMPLATES.some((entry) => entry.id === templateId)) throw new WorkflowError(409, "template_builtin", "Built-in teams cannot be deleted.");
    if (!this.state.teamTemplates?.[templateId]) throw new WorkflowError(404, "template_not_found", "That team template no longer exists.");
    delete this.state.teamTemplates[templateId];
    this.save();
  }

  /**
   * Set the PO's tool and policy choices for a role. Turned-off office tools stop working at
   * once; MCP servers and the risk policy reach a running role when it restarts.
   */
  async setRoleToolSettings(
    branchId: string,
    roleInput: unknown,
    input: unknown,
    availableTools: string[],
    restart: boolean,
  ): Promise<BranchView> {
    const branch = this.branch(branchId);
    const role = this.requireRole(branchId, roleInput);
    const parsed = parseRoleToolSettings(input, role, availableTools);
    if ("error" in parsed) throw new WorkflowError(400, "invalid_tool_settings", parsed.error);
    const previous = branch.runtime.roleToolSettings?.[role] ?? {};
    const sessionChanged =
      JSON.stringify(previous.mcpServers ?? []) !== JSON.stringify(parsed.settings.mcpServers ?? []) ||
      (previous.riskPolicy ?? "ask") !== (parsed.settings.riskPolicy ?? "ask");
    branch.runtime.roleToolSettings = { ...branch.runtime.roleToolSettings };
    // Only changes a session must restart for move the "out of date" mark.
    branch.runtime.roleToolSettings[role] = { ...parsed.settings, updatedAt: sessionChanged ? this.clock() : previous.updatedAt };
    const summary = [
      `${parsed.settings.disabledTools?.length ?? 0} office tool(s) off`,
      `${parsed.settings.mcpServers?.length ?? 0} extra MCP server(s)`,
      `risk policy: ${RISK_POLICY_LABELS[parsed.settings.riskPolicy ?? "ask"].toLowerCase()}`,
    ].join(", ");
    this.recordEvent(branchId, "po", "role_tools", `PO updated the ${this.roleName(branchId, role)}'s tools and policies (${summary}).`);
    this.save();
    if (restart && (this.persistentLink(branchId, role) || role === COORDINATOR_ROLE)) {
      await this.rolloverRole(branchId, role, "PO changed this role's tools and policies.");
    }
    return this.view(branchId);
  }

  /** Set the PO's instructions for a role; a running role picks them up when it restarts. */
  async setRoleInstructions(branchId: string, roleInput: unknown, textInput: unknown, restart: boolean): Promise<BranchView> {
    const branch = this.branch(branchId);
    const role = this.requireRole(branchId, roleInput);
    const instructions = text(textInput, "instructions", { required: false, max: MAX_CUSTOM_INSTRUCTIONS });
    branch.runtime.roleCustomInstructions = { ...branch.runtime.roleCustomInstructions };
    if (instructions) branch.runtime.roleCustomInstructions[role] = instructions;
    else delete branch.runtime.roleCustomInstructions[role];
    this.recordEvent(branchId, "po", "role_instructions", `PO ${instructions ? "updated" : "cleared"} the ${this.roleName(branchId, role)}'s instructions.`);
    this.save();
    if (restart && (this.persistentLink(branchId, role) || role === COORDINATOR_ROLE)) {
      await this.rolloverRole(branchId, role, "PO updated this role's instructions.");
    }
    return this.view(branchId);
  }

  /**
   * Close temporary workers the PO no longer needs: stop any turn, archive the session in
   * Omnigent, and retire it here. Their tasks stay as they are for the PM to redispatch.
   */
  async closeTemporaryWorkers(branchId: string, sessionIdsInput: unknown): Promise<{ closed: string[]; failed: string[] }> {
    this.branch(branchId);
    const sessionIds = requestedSessionIds(sessionIdsInput, "Choose a temporary worker to close.");
    const live = this.liveLinks(branchId);
    const links = sessionIds.map((sessionId) => live.find((link) => link.sessionId === sessionId && link.temporary));
    if (links.some((link) => !link)) throw new WorkflowError(404, "not_temporary", "Only live temporary workers of this room can be closed.");
    return this.archiveLinks(branchId, links as StoredRoleSession[], (link) => {
      const task = link.taskId ? this.state.tasks[link.taskId] : undefined;
      const open = task && task.status !== "done" && task.status !== "cancelled";
      this.recordEvent(
        branchId,
        "po",
        "temporary_closed",
        `PO closed the temporary ${this.roleName(branchId, link.role)}${task ? ` for "${task.title}"` : ""}${open ? `; the task is still ${task.status.replace("_", " ")}` : ""}.`,
        link.taskId,
      );
    });
  }

  /** Close replaced sessions (a role's earlier sessions, retired by a restart or handoff) and archive them in Omnigent. */
  async closeReplacedSessions(branchId: string, sessionIdsInput: unknown): Promise<{ closed: string[]; failed: string[] }> {
    this.branch(branchId);
    const sessionIds = requestedSessionIds(sessionIdsInput, "Choose a replaced session to close.");
    const known = this.links(branchId);
    const links = sessionIds.map((sessionId) => known.find((link) => link.sessionId === sessionId));
    if (links.some((link) => !link || link.retiredAt === null)) {
      throw new WorkflowError(404, "not_replaced", "Only replaced sessions of this room can be closed; a role's current session restarts instead.");
    }
    const result = await this.archiveLinks(branchId, links as StoredRoleSession[], () => undefined);
    if (result.closed.length) {
      const titles = [...new Set((links as StoredRoleSession[]).filter((link) => result.closed.includes(link.sessionId)).map((link) => this.roleName(branchId, link.role)))];
      this.recordEvent(branchId, "po", "replaced_closed", `PO closed ${result.closed.length} replaced session${result.closed.length === 1 ? "" : "s"} (${titles.join(", ")}).`);
      this.save();
    }
    return result;
  }

  /** Stop and archive role sessions in Omnigent, retiring their links. */
  private async archiveLinks(branchId: string, links: StoredRoleSession[], onClosed: (link: StoredRoleSession) => void): Promise<{ closed: string[]; failed: string[] }> {
    const closed: string[] = [];
    const failed: string[] = [];
    for (const link of links) {
      await this.omnigent.interrupt(link.sessionId).catch(() => undefined);
      try {
        await this.omnigent.archiveSession(link.sessionId);
      } catch (error) {
        failed.push(`${this.roleName(branchId, link.role)}: ${(error as Error).message}`);
        continue;
      }
      link.retiredAt ??= this.clock();
      closed.push(link.sessionId);
      onClosed(link);
    }
    this.save();
    return { closed, failed };
  }

  /** The role's current (persistent) session, if it has started. */
  roleSessionId(branchId: string, role: AgentRoleId): string | null {
    return this.persistentLink(branchId, role)?.sessionId ?? null;
  }

  /** A role's live session with its office gate policies, and the spec a new session would get. */
  roleToolContext(branchId: string, role: AgentRoleId): RoleToolContext {
    const branch = this.branch(branchId);
    const link = this.persistentLink(branchId, role);
    const gatePolicies = link
      ? Object.entries(link.gatePolicies).map(([gate, policy]) => gatePolicyName(gate as GateType, policy?.version))
      : (Object.keys(RUNTIME_GATE_PATTERNS) as GateType[]).filter((gate) => branch.gates[gate]).map((gate) => gatePolicyName(gate));
    const plannedSpec = roleAgentSpec({ role, projectName: "", runtime: branch.runtime, mcpUrl: this.mcpUrl, mcpToken: "" });
    return {
      sessionId: link?.sessionId ?? null,
      sessionStartedAt: link?.createdAt ?? null,
      settings: branch.runtime.roleToolSettings?.[role] ?? {},
      gatePolicies,
      plannedSpec,
    };
  }

  callerForToken(token: string): McpCaller | null {
    if (!token) return null;
    const link = (this.state.roleSessions as StoredRoleSession[]).find((entry) => entry.mcpToken === token);
    if (!link || link.retiredAt !== null) return null;
    const branch = this.state.branches[link.branchId];
    return branch ? { link, branch } : null;
  }

  projectLinks(projectId: string): ProjectLink[] {
    return this.state.projectLinks[projectId] ?? [];
  }

  /** Add a result link, or update the one with the same URL. */
  addProjectLink(projectId: string, input: unknown, addedBy: ProjectLink["addedBy"]): ProjectLink {
    const parsed = parseLinkInput(input);
    if ("error" in parsed) throw new WorkflowError(400, "invalid_link", parsed.error);
    const links = [...this.projectLinks(projectId)];
    const index = links.findIndex((entry) => entry.url === parsed.url);
    if (index < 0 && links.length >= MAX_LINKS_PER_PROJECT) {
      throw new WorkflowError(409, "too_many_links", `A project keeps at most ${MAX_LINKS_PER_PROJECT} links; remove one first.`);
    }
    const link: ProjectLink = { id: index >= 0 ? links[index].id : newId("lnk"), ...parsed, addedBy, at: this.clock() };
    if (index >= 0) links[index] = link;
    else links.push(link);
    this.state.projectLinks[projectId] = links;
    const branch = this.activeBranch(projectId);
    if (branch) this.recordEvent(branch.id, addedBy, "link_shared", `${this.roleName(branch.id, addedBy)} shared a link: ${link.label} (${link.url})`);
    this.save();
    return link;
  }

  removeProjectLink(projectId: string, linkId: string): void {
    const links = this.projectLinks(projectId);
    const removed = links.find((entry) => entry.id === linkId);
    if (!removed) throw new WorkflowError(404, "link_not_found", "That link was already removed.");
    this.state.projectLinks[projectId] = links.filter((entry) => entry.id !== linkId);
    const branch = this.activeBranch(projectId);
    if (branch) this.recordEvent(branch.id, "po", "link_removed", `PO removed the link ${removed.label}.`);
    this.save();
  }

  /** A role shares a result, such as a deployment URL or a pull request, with the PO. */
  publishLink(caller: McpCaller, args: Record<string, unknown>): ProjectLink {
    return this.addProjectLink(caller.branch.projectId, args, caller.link.role);
  }

  isRelayedItem(itemId: string): boolean {
    return this.relayedSet.has(itemId);
  }

  private relayedCache: { length: number; set: Set<string> } | null = null;

  private get relayedSet(): Set<string> {
    const ids = this.state.relayedItemIds;
    if (!this.relayedCache || this.relayedCache.length !== ids.length) {
      this.relayedCache = { length: ids.length, set: new Set(ids) };
    }
    return this.relayedCache.set;
  }

  /** Requests waiting on the PO in a project, across all of its branches. */
  pendingHumanRequests(projectId: string): number {
    let count = 0;
    for (const branch of Object.values(this.state.branches)) {
      if (branch.projectId !== projectId) continue;
      count += Object.values(this.state.gates).filter((gate) => gate.branchId === branch.id && gate.status === "pending").length;
      count += Object.values(this.state.proposals).filter((proposal) => proposal.branchId === branch.id && proposal.status === "pending").length;
      count += Object.values(this.state.interviews).filter((question) => question.branchId === branch.id && question.status === "asked").length;
      count += Object.values(this.state.tasks).filter(
        (task) => task.branchId === branch.id && task.status === "blocked" && task.responsible.includes("po"),
      ).length;
    }
    return count;
  }

  /** Latest PO decision (gate or proposal) in a project; these are meaningful interactions. */
  latestDecisionAt(projectId: string): number | null {
    let latest: number | null = null;
    const branchIds = new Set(Object.values(this.state.branches).filter((branch) => branch.projectId === projectId).map((branch) => branch.id));
    // Only PO decisions set decidedAt; a decision still counts after it goes stale.
    for (const record of [...Object.values(this.state.gates), ...Object.values(this.state.proposals)]) {
      if (branchIds.has(record.branchId) && record.decidedAt !== null) latest = Math.max(latest ?? 0, record.decidedAt);
    }
    return latest;
  }

  /** Role identity for a session, from durable links. */
  roleOfSession(sessionId: string): RoleSessionLink | null {
    const link = this.state.roleSessions.find((entry) => entry.sessionId === sessionId);
    return link ? publicLink(link as StoredRoleSession) : null;
  }

  view(branchId: string): BranchView {
    const branch = this.branch(branchId);
    const byBranch = <T extends { branchId: string }>(records: Record<string, T>) =>
      Object.values(records).filter((record) => record.branchId === branchId);
    return {
      branch,
      roleSessions: this.links(branchId).map(publicLink),
      board: this.state.boards[branchId],
      proposals: byBranch(this.state.proposals).sort((left, right) => right.createdAt - left.createdAt),
      tasks: byBranch(this.state.tasks).sort((left, right) => left.createdAt - right.createdAt),
      gates: byBranch(this.state.gates).sort((left, right) => right.createdAt - left.createdAt),
      events: this.state.events.filter((event) => event.branchId === branchId).slice(-100).reverse(),
      interviews: byBranch(this.state.interviews).sort((left, right) => left.addedAt - right.addedAt),
      currentInterview: this.currentInterview(branchId),
      decisions: byBranch(this.state.decisions).sort((left, right) => right.at - left.at),
    };
  }

  // ---- Branch lifecycle -------------------------------------------------

  async assignTheme(projectId: string, input: AssignThemeInput): Promise<BranchView> {
    if (this.activeBranch(projectId)) {
      throw new WorkflowError(409, "theme_already_assigned", "This project already has an SDLC theme branch.");
    }
    const runtime = normalizeRuntime(input.runtime, this.toolsFor ?? undefined);
    const gates = { ...DEFAULT_GATES };
    for (const [gate, enabled] of Object.entries(input.gates ?? {})) {
      if (!(gate in gates) || typeof enabled !== "boolean") throw new WorkflowError(400, "invalid_gate", `Unknown gate setting ${gate}`);
      gates[gate as GateType] = enabled;
    }
    const projects = await this.omnigent.listProjects();
    const project = projects.find((entry) => entry.id === projectId);
    if (!project) throw new WorkflowError(404, "room_not_found", "No such project");
    const now = this.clock();
    const branch: ThemeBranch = {
      id: newId("branch"),
      projectId,
      theme: "sdlc",
      themeRevision: THEME_REVISION,
      status: "active",
      createdAt: now,
      runtime,
      gates,
      gatePolicyRevision: gatePolicyRevision(gates),
    };
    this.state.branches[branch.id] = branch;
    this.state.boards[branch.id] = {
      branchId: branch.id,
      revision: 0,
      digest: boardDigest([]),
      requirements: [],
      updatedAt: now,
      updatedBy: "po",
    };
    this.recordEvent(branch.id, "office", "theme_assigned", `SDLC theme assigned (revision ${THEME_REVISION}).`);
    this.save();
    try {
      await this.ensureRoleSession(branch, COORDINATOR_ROLE, project.name);
    } catch (error) {
      this.recordEvent(branch.id, "office", "coordinator_failed", `Could not start the PM: ${(error as Error).message}`);
      this.save();
      throw new WorkflowError(502, "coordinator_start_failed", `Theme assigned, but the PM could not start: ${(error as Error).message}`);
    }
    return this.view(branch.id);
  }

  private async project(projectId: string): Promise<{ id: string; name: string; config?: { host_id?: string; workspace?: string } } | undefined> {
    const projects = await this.omnigent.listProjects();
    return projects.find((entry) => entry.id === projectId);
  }

  private async projectName(projectId: string): Promise<string> {
    return (await this.project(projectId))?.name ?? projectId;
  }

  async ensureRoleSession(
    branch: ThemeBranch,
    role: AgentRoleId,
    projectName?: string,
    options: { temporaryTask?: WorkflowTask; handoffFrom?: StoredRoleSession } = {},
  ): Promise<StoredRoleSession> {
    if (!options.temporaryTask && !options.handoffFrom) {
      const existing = this.persistentLink(branch.id, role);
      if (existing) return existing;
    }
    const needProject = !projectName || (Boolean(branch.runtime.hostId) && !branch.runtime.workspace);
    const project = needProject ? await this.project(branch.projectId) : undefined;
    const name = projectName ?? project?.name ?? branch.projectId;
    if (branch.runtime.hostId && !branch.runtime.workspace && project?.config?.workspace) {
      branch.runtime.workspace = project.config.workspace;
      this.save();
    }
    const mcpToken = randomBytes(24).toString("hex");
    const bundle = roleBundle({
      role,
      projectName: name,
      runtime: branch.runtime,
      mcpUrl: this.mcpUrl,
      mcpToken,
      temporaryTaskTitle: options.temporaryTask?.title,
    });
    const title = options.temporaryTask
      ? `${this.roleName(branch.id, role)} · ${options.temporaryTask.title}`.slice(0, 200)
      : `${this.roleName(branch.id, role)} · SDLC`;
    const metadata: Record<string, unknown> = {
      project_id: branch.projectId,
      title,
      labels: {
        "office.branch": branch.id,
        "office.theme": branch.theme,
        "office.role": role,
        "office.temporary": options.temporaryTask ? "true" : "false",
      },
    };
    if (branch.runtime.hostId) metadata.host_id = branch.runtime.hostId;
    if (branch.runtime.workspace) metadata.workspace = branch.runtime.workspace;
    const failureKey = `${branch.id}:${role}`;
    let created: { session_id: string };
    try {
      created = await this.omnigent.createBundledSession(bundle, metadata);
      if (branch.runtime.runnerId) await this.omnigent.bindRunner(created.session_id, branch.runtime.runnerId);
    } catch (error) {
      // Kept so the room can say why the role is missing instead of looking idle.
      const harness = roleHarness(branch.runtime, role);
      const code = (error as { code?: string }).code ?? "role_start_failed";
      this.state.roleStartFailures[failureKey] = { branchId: branch.id, role, harness, at: this.clock(), code, message: (error as Error).message };
      this.recordEvent(branch.id, "office", "role_start_failed", `${this.roleName(branch.id, role)} could not start on ${harness}: ${(error as Error).message}`);
      this.save();
      throw error;
    }
    delete this.state.roleStartFailures[failureKey];
    const link: StoredRoleSession = {
      branchId: branch.id,
      role,
      sessionId: created.session_id,
      temporary: Boolean(options.temporaryTask),
      taskId: options.temporaryTask?.id ?? null,
      createdAt: this.clock(),
      retiredAt: null,
      handoffFromSessionId: options.handoffFrom?.sessionId ?? null,
      mcpToken,
      gatePolicies: {},
    };
    this.state.roleSessions.push(link);
    this.recordEvent(
      branch.id,
      "office",
      "role_started",
      `${this.roleName(branch.id, role)} session started${options.temporaryTask ? ` as a temporary worker for "${options.temporaryTask.title}"` : ""}.`,
      options.temporaryTask?.id ?? null,
    );
    this.save();
    await this.installGatePolicies(branch, link);
    return link;
  }

  /** Install or replace the runtime gate policies on one role session. */
  private async installGatePolicies(branch: ThemeBranch, link: StoredRoleSession): Promise<void> {
    for (const gate of Object.keys(RUNTIME_GATE_PATTERNS) as GateType[]) {
      if (!branch.gates[gate]) continue;
      const expression = gatePolicyExpression(gate, this.approvedOperations(branch.id, gate));
      if (!expression) continue;
      const previous = link.gatePolicies[gate];
      const version = (previous?.version ?? 0) + 1;
      // Add the replacement before removing the old policy so the gate never lapses.
      const created = await this.omnigent.addSessionPolicy(link.sessionId, gatePolicyName(gate, version), expression);
      link.gatePolicies[gate] = { id: created.id, version };
      this.save();
      if (previous) await this.omnigent.deleteSessionPolicy(link.sessionId, previous.id).catch(() => undefined);
    }
  }

  private approvedOperations(branchId: string, gate: GateType): string[] {
    return Object.values(this.state.gates)
      .filter((request) => request.branchId === branchId && request.gate === gate && request.status === "approved" && request.operation)
      .filter((request) => {
        const task = request.taskId ? this.state.tasks[request.taskId] : null;
        return !task || task.status !== "done";
      })
      .map((request) => request.operation as string);
  }

  private async refreshGatePolicies(branch: ThemeBranch): Promise<void> {
    for (const link of this.liveLinks(branch.id)) {
      try {
        await this.installGatePolicies(branch, link);
      } catch (error) {
        this.recordEvent(branch.id, "office", "policy_update_failed", `Could not update gate policy on ${this.roleName(branch.id, link.role)}: ${(error as Error).message}`);
      }
    }
    this.save();
  }

  /** Send agent-originated text; recorded so it never counts as human activity. */
  private async relay(sessionId: string, message: string): Promise<void> {
    const stableId = randomBytes(16).toString("hex");
    this.state.relayedItemIds.push(stableId);
    if (this.state.relayedItemIds.length > MAX_RELAYED_IDS) this.state.relayedItemIds.splice(0, this.state.relayedItemIds.length - MAX_RELAYED_IDS);
    this.save();
    await this.omnigent.sendMessage(sessionId, message, stableId);
  }

  private async relayToRole(branch: ThemeBranch, role: RoleId, message: string): Promise<boolean> {
    if (!hasRole(teamOf(branch.runtime), role)) return false;
    const link = this.persistentLink(branch.id, role);
    if (!link) return false;
    try {
      await this.relay(link.sessionId, message);
      return true;
    } catch (error) {
      this.recordEvent(branch.id, "office", "relay_failed", `Could not reach ${this.roleName(branch.id, role)}: ${(error as Error).message}`);
      return false;
    }
  }

  private recordEvent(branchId: string, actor: RoleId | "office", kind: string, summary: string, taskId: string | null = null): WorkflowEvent {
    const event: WorkflowEvent = { id: newId("evt"), branchId, at: this.clock(), actor, kind, summary, taskId };
    this.state.events.push(event);
    const branchEvents = this.state.events.filter((entry) => entry.branchId === branchId);
    if (branchEvents.length > MAX_EVENTS_PER_BRANCH) {
      const drop = new Set(branchEvents.slice(0, branchEvents.length - MAX_EVENTS_PER_BRANCH).map((entry) => entry.id));
      this.state.events = this.state.events.filter((entry) => !drop.has(entry.id));
    }
    return event;
  }

  private attention(branch: ThemeBranch, title: string, body: string): void {
    void this.projectName(branch.projectId)
      .then((name) => this.notifier.notify(`${name}: ${title}`, body))
      .catch(() => undefined);
  }

  // ---- PO operations (office HTTP API) ----------------------------------

  async updateBoard(branchId: string, baseRevision: unknown, requirementsInput: unknown): Promise<BranchView> {
    const branch = this.branch(branchId);
    const board = this.state.boards[branchId];
    if (baseRevision !== board.revision) {
      throw new WorkflowError(409, "board_conflict", `The board changed (now revision ${board.revision}). Reload before editing.`);
    }
    const requirements = normalizeRequirements(requirementsInput);
    this.applyBoard(branch, requirements, "po");
    this.save();
    await this.relayToRole(
      branch,
      COORDINATOR_ROLE,
      `[Office] The PO updated the requirements board to revision ${this.state.boards[branchId].revision}. Read it with office_board_get.`,
    );
    return this.view(branchId);
  }

  private applyBoard(branch: ThemeBranch, requirements: Requirement[], actor: RoleId): void {
    const board = this.state.boards[branch.id];
    const digest = boardDigest(requirements);
    if (digest === board.digest) return;
    const next: RequirementsBoard = {
      branchId: branch.id,
      revision: board.revision + 1,
      digest,
      requirements,
      updatedAt: this.clock(),
      updatedBy: actor,
    };
    this.state.boards[branch.id] = next;
    this.recordEvent(branch.id, actor, "board_updated", `Requirements board revision ${next.revision} (${requirements.length} requirements).`);
    for (const proposal of Object.values(this.state.proposals)) {
      if (proposal.branchId === branch.id && proposal.status === "pending" && proposal.baseRevision !== next.revision) {
        proposal.status = "stale";
        proposal.decidedAt = null;
      }
    }
    // A requirements change invalidates approvals made against the old board.
    for (const gate of Object.values(this.state.gates)) {
      if (gate.branchId !== branch.id || (gate.status !== "pending" && gate.status !== "approved")) continue;
      const boardBound = gate.artifact.kind === "board" && gate.artifact.digest !== digest;
      const builtOnOldBoard = gate.gate === "architecture" && gate.artifact.boardRevision !== null && gate.artifact.boardRevision < next.revision;
      if (boardBound || builtOnOldBoard) this.markStale(branch, gate, `Requirements changed to revision ${next.revision}.`);
    }
  }

  private markStale(branch: ThemeBranch, gate: GateRequest, reason: string): void {
    if (gate.status !== "pending" && gate.status !== "approved") return;
    gate.status = "stale";
    gate.statusReason = reason;
    this.recordEvent(branch.id, "office", "gate_stale", `${GATE_TITLES[gate.gate]} gate is stale: ${reason}`, gate.taskId);
    if (gate.taskId) {
      const task = this.state.tasks[gate.taskId];
      if (task && task.status === "awaiting_gate") task.status = "in_progress";
    }
    for (const dependent of GATE_INVALIDATES[gate.gate] ?? []) {
      for (const other of Object.values(this.state.gates)) {
        if (other.branchId === branch.id && other.gate === dependent && other.createdAt >= gate.createdAt) {
          this.markStale(branch, other, `${GATE_TITLES[gate.gate]} approval it depended on became stale.`);
        }
      }
    }
    if (gate.requestedBySessionId) {
      void this.relay(gate.requestedBySessionId, `[Office] ${GATE_TITLES[gate.gate]} gate ${gate.id} is stale: ${reason} Request it again when ready.`).catch(() => undefined);
    }
  }

  async decideProposal(branchId: string, proposalId: string, action: unknown, feedback: unknown): Promise<BranchView> {
    const branch = this.branch(branchId);
    const proposal = this.state.proposals[proposalId];
    if (!proposal || proposal.branchId !== branchId) throw new WorkflowError(404, "proposal_not_found", "No such proposal");
    if (action !== "accept" && action !== "reject") throw new WorkflowError(400, "invalid_action", "action must be accept or reject");
    const wanted = action === "accept" ? "accepted" : "rejected";
    if (proposal.status === wanted) return this.view(branchId);
    if (proposal.status !== "pending") {
      throw new WorkflowError(409, proposal.status === "stale" ? "stale_request" : "already_decided", `This proposal is ${proposal.status}.`);
    }
    const board = this.state.boards[branchId];
    if (action === "accept" && proposal.baseRevision !== board.revision) {
      proposal.status = "stale";
      this.save();
      throw new WorkflowError(409, "stale_request", "The board changed after this proposal was made.");
    }
    proposal.status = wanted;
    proposal.decidedAt = this.clock();
    const note = text(feedback, "feedback", { required: false });
    if (action === "accept") this.applyBoard(branch, proposal.requirements, "po");
    this.recordEvent(branchId, "po", `proposal_${wanted}`, `PO ${wanted} the PM's board proposal.${note ? ` Feedback: ${note}` : ""}`);
    this.save();
    await this.relayToRole(
      branch,
      COORDINATOR_ROLE,
      `[Office] The PO ${wanted} your board proposal ${proposal.id}.${note ? ` Feedback: ${note}` : ""}`,
    );
    return this.view(branchId);
  }

  /**
   * Decide a workflow gate. The decision is bound to the artifact digest the
   * PO reviewed; a changed artifact or a non-pending request is rejected, and
   * repeating the same decision is a no-op.
   */
  async decideGate(
    branchId: string,
    gateId: string,
    input: { action: unknown; digest: unknown; feedback: unknown },
  ): Promise<{ view: BranchView; replayed: boolean }> {
    const branch = this.branch(branchId);
    const gate = this.state.gates[gateId];
    if (!gate || gate.branchId !== branchId) throw new WorkflowError(404, "gate_not_found", "No such gate request");
    if (input.action !== "approve" && input.action !== "request_changes") {
      throw new WorkflowError(400, "invalid_action", "action must be approve or request_changes");
    }
    const wanted = input.action === "approve" ? "approved" : "changes_requested";
    if (gate.status === wanted) return { view: this.view(branchId), replayed: true };
    if (gate.status !== "pending") {
      const code = gate.status === "approved" || gate.status === "changes_requested" ? "already_decided" : "stale_request";
      throw new WorkflowError(409, code, `This request is ${gate.status.replace("_", " ")}.`);
    }
    if (input.digest !== gate.artifact.digest) {
      throw new WorkflowError(409, "stale_request", "The artifact you reviewed is not the one in this request. Reload it.");
    }
    if (gate.artifact.kind === "board" && this.state.boards[branchId].digest !== gate.artifact.digest) {
      this.markStale(branch, gate, "The requirements board changed after the request.");
      this.save();
      throw new WorkflowError(409, "stale_request", "The requirements board changed after this request.");
    }
    if (gate.gatePolicyRevision !== branch.gatePolicyRevision) {
      this.markStale(branch, gate, "The gate policy changed after the request.");
      this.save();
      throw new WorkflowError(409, "stale_request", "The gate policy changed after this request.");
    }
    const feedback = text(input.feedback, "feedback", { required: false });
    if (wanted === "changes_requested" && !feedback) {
      throw new WorkflowError(400, "feedback_required", "Describe the changes you need.");
    }
    gate.status = wanted;
    gate.decidedAt = this.clock();
    gate.feedback = feedback || null;
    const task = gate.taskId ? this.state.tasks[gate.taskId] : undefined;
    if (task) {
      if (wanted === "approved" && task.accountable === "po") {
        task.status = "done";
        task.evidence.push({ at: gate.decidedAt, role: "po", summary: "PO approved the gate.", references: [gate.artifact.ref] });
      } else {
        task.status = wanted === "approved" ? "submitted" : "in_progress";
      }
      task.updatedAt = gate.decidedAt;
    }
    this.recordEvent(
      branchId,
      "po",
      `gate_${wanted}`,
      `PO ${wanted === "approved" ? "approved" : "requested changes on"} the ${GATE_TITLES[gate.gate]} gate${feedback ? `: ${feedback}` : "."}`,
      gate.taskId,
    );
    this.save();
    if (gate.operation) await this.refreshGatePolicies(branch);
    const outcome =
      wanted === "approved"
        ? `[Office] The PO approved the ${GATE_TITLES[gate.gate]} gate ${gate.id}.${gate.operation ? ` The runtime now allows exactly: ${gate.operation}` : ""}${feedback ? ` Note: ${feedback}` : ""} Runtime policies still apply.`
        : `[Office] The PO requested changes on the ${GATE_TITLES[gate.gate]} gate ${gate.id}: ${feedback}`;
    if (gate.requestedBySessionId) await this.relay(gate.requestedBySessionId, outcome).catch(() => undefined);
    const coordinator = this.persistentLink(branchId, COORDINATOR_ROLE);
    if (coordinator && coordinator.sessionId !== gate.requestedBySessionId) {
      await this.relay(coordinator.sessionId, outcome).catch(() => undefined);
    }
    return { view: this.view(branchId), replayed: false };
  }

  async answerInterview(branchId: string, questionId: string, answerInput: unknown): Promise<{ view: BranchView; text: string; sessionId: string | null }> {
    const branch = this.branch(branchId);
    const question = this.state.interviews[questionId];
    if (!question || question.branchId !== branchId) throw new WorkflowError(404, "question_not_found", "No such question");
    const answer = text(answerInput, "answer", { max: 10_000 });
    if (question.status === "answered") {
      if (question.answer === answer) return { view: this.view(branchId), text: "", sessionId: null };
      throw new WorkflowError(409, "already_decided", "This question was already answered.");
    }
    if (question.status !== "asked") throw new WorkflowError(409, "not_asked", "The PM has not asked this question yet.");
    question.status = "answered";
    question.answer = answer;
    question.answeredAt = this.clock();
    this.recordEvent(branchId, "po", "interview_answered", `PO answered: ${question.question}`);
    this.save();
    // The answer is the PO's own words, so it goes to the PM as a human message.
    const coordinator = this.persistentLink(branch.id, COORDINATOR_ROLE);
    return {
      view: this.view(branchId),
      text: `Answer to your interview question "${question.question}":\n\n${answer}`,
      sessionId: coordinator?.sessionId ?? null,
    };
  }

  coordinatorSession(branchId: string): string | null {
    return this.persistentLink(branchId, COORDINATOR_ROLE)?.sessionId ?? null;
  }

  async ensureCoordinator(branchId: string): Promise<string> {
    const existing = this.coordinatorSession(branchId);
    if (existing) return existing;
    const link = await this.ensureRoleSession(this.branch(branchId), COORDINATOR_ROLE);
    return link.sessionId;
  }

  /** Start a new interview: unasked questions of the previous one are skipped. */
  interviewKickoff(branchId: string, topic: unknown): { sessionId: string; text: string } {
    this.branch(branchId);
    const sessionId = this.coordinatorSession(branchId);
    if (!sessionId) throw new WorkflowError(409, "no_coordinator", "The PM is not running.");
    const focus = text(topic, "topic", { required: false, max: 2000 });
    const open = this.openQuestion(branchId);
    if (open) throw new WorkflowError(409, "question_pending", `Answer the PM's open question first: ${open.question}`);
    const now = this.clock();
    const previous = this.currentInterview(branchId);
    if (previous) {
      previous.endedAt = now;
      for (const question of this.roundQuestions(previous.id)) {
        if (question.status === "planned") Object.assign(question, { status: "skipped", skipReason: "A new interview was started." });
      }
    }
    const round: InterviewRound = { id: newId("interview"), branchId, topic: focus, startedAt: now, endedAt: null };
    this.state.interviewRounds[round.id] = round;
    this.recordEvent(branchId, "po", "interview_started", focus ? `PO started an interview: ${focus}` : "PO started an interview.");
    this.save();
    return {
      sessionId,
      text:
        "Please interview me about the requirements" +
        (focus ? ` for: ${focus}` : "") +
        ". First list every question you plan to ask with office_interview_plan. Then ask them one at a time with " +
        "office_interview_ask (pass the planned questionId) and wait for my answer. Add follow-up questions with " +
        "office_interview_plan as they come up, skip ones my answers made unnecessary with office_interview_skip, " +
        "and propose board updates with office_board_propose.",
    };
  }

  async stopBranch(branchId: string): Promise<{ stopped: number; failed: string[] }> {
    this.branch(branchId);
    let stopped = 0;
    const failed: string[] = [];
    // Stop takes effect immediately, without coordinator routing.
    await Promise.all(
      this.liveLinks(branchId).map(async (link) => {
        try {
          await this.omnigent.interrupt(link.sessionId);
          stopped++;
        } catch {
          failed.push(link.sessionId);
        }
      }),
    );
    this.recordEvent(branchId, "po", "stopped", `PO stopped ${stopped} role session(s).`);
    this.save();
    return { stopped, failed };
  }

  /** Replace a role's session through a recorded handoff, e.g. after a coordinator failure. */
  async rolloverRole(branchId: string, roleInput: unknown, reasonInput: unknown): Promise<BranchView> {
    const branch = this.branch(branchId);
    const role = this.requireRole(branchId, roleInput);
    const reason = text(reasonInput, "reason", { required: false, max: 2000 }) || "PO requested a fresh session.";
    const previous = this.persistentLink(branchId, role);
    const next = await this.ensureRoleSession(branch, role, undefined, { handoffFrom: previous ?? undefined });
    if (previous) {
      previous.retiredAt = this.clock();
      await this.omnigent.interrupt(previous.sessionId).catch(() => undefined);
    }
    this.recordEvent(branchId, "office", "handoff", `${this.roleName(branchId, role)} rolled over to a new session (${reason}).`);
    this.save();
    await this.relay(next.sessionId, this.handoffBrief(branch, role, previous, reason));
    return this.view(branchId);
  }

  private handoffBrief(branch: ThemeBranch, role: AgentRoleId, previous: StoredRoleSession | undefined, reason: string): string {
    const tasks = Object.values(this.state.tasks).filter(
      (task) => task.branchId === branch.id && ACTIVE_TASK_STATUSES.includes(task.status) && (role === COORDINATOR_ROLE || task.responsible.includes(role) || task.accountable === role),
    );
    const lines = [
      `[Office handoff] You are taking over the ${this.roleName(branch.id, role)} role${previous ? ` from session ${previous.sessionId}` : ""}. Reason: ${reason}`,
      `Requirements board revision ${this.state.boards[branch.id].revision}. Read the full state with office_board_get.`,
      tasks.length ? "Open tasks:" : "No open tasks.",
      ...tasks.map((task) => `- ${task.id} [${task.status}] ${task.title}`),
    ];
    const interview = role === COORDINATOR_ROLE ? this.currentInterview(branch.id) : null;
    if (interview) {
      const questions = this.roundQuestions(interview.id);
      const open = questions.find((question) => question.status === "asked");
      const planned = questions.filter((question) => question.status === "planned");
      lines.push(
        `An interview${interview.topic ? ` about "${interview.topic}"` : ""} is in progress.`,
        open ? `The PO has not answered yet: ${open.question} (wait for the answer; do not ask it again).` : "No question is waiting for the PO.",
        planned.length ? "Planned questions still to ask:" : "No planned questions remain.",
        ...planned.map((question) => `- ${question.id}: ${question.question}`),
      );
    }
    return lines.join("\n");
  }

  /** Move roles to another harness (and optionally model), restarting each with a handoff. */
  async switchRoleHarness(
    branchId: string,
    rolesInput: unknown,
    harnessInput: unknown,
    modelInput: unknown,
    reasonInput: unknown,
    effortInput: unknown = "",
  ): Promise<BranchView> {
    const branch = this.branch(branchId);
    const team = teamOf(branch.runtime);
    const roles = Array.isArray(rolesInput) ? [...new Set(rolesInput.filter((role): role is string => hasRole(team, role)))] : [];
    if (roles.length === 0) throw new WorkflowError(400, "invalid_role", "Choose at least one role to switch.");
    const harness = text(harnessInput, "harness", { max: 100 });
    const model = text(modelInput, "model", { required: false, max: 200 });
    const reason = text(reasonInput, "reason", { required: false, max: 2000 }) || "PO switched the harness.";
    if (effortInput !== "" && effortInput !== undefined && effortInput !== null && !isEffort(effortInput)) {
      throw new WorkflowError(400, "invalid_effort", `effort must be one of ${EFFORT_ORDER.join(", ")}`);
    }
    const effort = isEffort(effortInput) ? effortInput : "";
    branch.runtime.roleHarnesses = { ...branch.runtime.roleHarnesses };
    branch.runtime.roleModels = { ...branch.runtime.roleModels };
    branch.runtime.roleEfforts = { ...branch.runtime.roleEfforts };
    const target = `${harness} (${model || "harness default model"}${effort ? `, ${effort} effort` : ""})`;
    for (const role of roles) {
      const from = roleHarness(branch.runtime, role);
      branch.runtime.roleHarnesses[role] = harness;
      // A model name rarely carries across providers, so the old override is dropped.
      if (model) branch.runtime.roleModels[role] = model;
      else delete branch.runtime.roleModels[role];
      if (effort) branch.runtime.roleEfforts[role] = effort;
      else delete branch.runtime.roleEfforts[role];
      const change = from === harness ? `now uses ${target}` : `moved from ${from} to ${target}`;
      this.recordEvent(branchId, "po", "harness_switched", `${this.roleName(branchId, role)} ${change}: ${reason}`);
    }
    this.save();
    const failures: string[] = [];
    for (const role of roles) {
      // Roles that never started pick up the new harness when the PM first dispatches to them.
      if (!this.persistentLink(branchId, role) && role !== COORDINATOR_ROLE) continue;
      await this.rolloverRole(branchId, role, `${reason} Now running on ${target}.`).catch((error: Error) => failures.push(`${this.roleName(branchId, role)}: ${error.message}`));
    }
    if (failures.length) throw new WorkflowError(502, "switch_incomplete", `Harness saved, but some roles did not restart. ${failures.join("; ")}`);
    return this.view(branchId);
  }

  /** Branches whose role sessions are watched for failures. */
  activeBranches(): ThemeBranch[] {
    return Object.values(this.state.branches).filter((branch) => branch.status !== "archived");
  }

  roleStartFailure(branchId: string, role: AgentRoleId): RoleStartFailure | null {
    return this.state.roleStartFailures[`${branchId}:${role}`] ?? null;
  }

  /** Record a PO recovery action in the room's event feed. */
  logRecovery(branchId: string, summary: string): void {
    this.logPoEvent(branchId, "recovery", summary);
  }

  /** Record something done on the PO's behalf, such as an automatic session approval. */
  logPoEvent(branchId: string, kind: string, summary: string): void {
    if (!this.state.branches[branchId]) return;
    this.recordEvent(branchId, "po", kind, summary);
    this.save();
  }

  /** The PM's interview is waiting on the PM: it has nothing open for the PO but has not finished. */
  interviewAwaitingCoordinator(branchId: string): { since: number; reason: string } | null {
    const interview = this.currentInterview(branchId);
    if (!interview) return null;
    const questions = this.roundQuestions(interview.id);
    if (questions.some((question) => question.status === "asked")) return null;
    if (questions.length === 0) return { since: interview.startedAt, reason: "The PM has not planned or asked any interview questions yet." };
    if (!questions.some((question) => question.status === "planned")) return null;
    const lastAnswer = Math.max(interview.startedAt, ...questions.map((question) => question.answeredAt ?? 0), ...questions.map((question) => question.addedAt));
    return { since: lastAnswer, reason: "The PM has planned questions left but has not asked the next one." };
  }

  async completeTaskAsPo(branchId: string, taskId: string, summaryInput: unknown): Promise<BranchView> {
    const branch = this.branch(branchId);
    const task = this.task(branchId, taskId);
    if (task.accountable !== "po") throw new WorkflowError(403, "not_accountable", "Only the accountable role completes this task.");
    if (task.status === "done") return this.view(branchId);
    if (task.gate && branch.gates[task.gate] && !this.hasApprovedGate(task)) {
      throw new WorkflowError(409, "gate_required", `Approve the ${GATE_TITLES[task.gate]} gate first.`);
    }
    const summary = text(summaryInput, "summary", { required: false }) || "Accepted by the PO.";
    task.evidence.push({ at: this.clock(), role: "po", summary, references: [] });
    await this.finishTask(branch, task, "po");
    return this.view(branchId);
  }

  // ---- Role operations (office MCP tools) -------------------------------

  /** Resolve a task by id, or by its exact title when that is unique in the branch. */
  private task(branchId: string, reference: unknown): WorkflowTask {
    if (typeof reference !== "string") throw new WorkflowError(400, "invalid_input", "taskId is required");
    const byId = this.state.tasks[reference];
    if (byId && byId.branchId === branchId) return byId;
    const byTitle = Object.values(this.state.tasks).filter((task) => task.branchId === branchId && task.title === reference);
    if (byTitle.length === 1) return byTitle[0];
    throw new WorkflowError(404, "task_not_found", byTitle.length > 1 ? `Several tasks are titled ${reference}; use the id.` : `No task ${reference}`);
  }

  private requireCoordinator(caller: McpCaller): void {
    if (caller.link.role !== COORDINATOR_ROLE) {
      throw new WorkflowError(403, "coordinator_only", "Only the PM coordinator may do this; ask the PM with office_message_role.");
    }
  }

  private hasApprovedGate(task: WorkflowTask): boolean {
    return Object.values(this.state.gates).some((gate) => gate.taskId === task.id && gate.gate === task.gate && gate.status === "approved");
  }

  boardSnapshot(caller: McpCaller): Record<string, unknown> {
    const view = this.view(caller.branch.id);
    const mine = view.tasks.filter(
      (task) => task.responsible.includes(caller.link.role) || task.accountable === caller.link.role,
    );
    return {
      you: { role: caller.link.role, title: this.roleName(caller.branch.id, caller.link.role), sessionId: caller.link.sessionId },
      board: view.board,
      gatesEnabled: caller.branch.gates,
      tasks: view.tasks.map((task) => ({
        id: task.id,
        taskType: task.taskType,
        title: task.title,
        status: task.status,
        responsible: task.responsible,
        accountable: task.accountable,
        dependsOn: task.dependsOn,
        gate: task.gate,
      })),
      yourTasks: mine,
      gates: view.gates.map((gate) => ({ id: gate.id, gate: gate.gate, taskId: gate.taskId, status: gate.status, feedback: gate.feedback, operation: gate.operation })),
      pendingInterviewQuestion: this.openQuestion(caller.branch.id)?.question ?? null,
      interview: this.interviewAgenda(caller.branch.id),
      recentDecisions: view.decisions.slice(0, 10),
      team: teamOf(caller.branch.runtime).roles.map((role) => ({ id: role.id, title: role.title })),
      matrix: teamOf(caller.branch.runtime).taskTypes.map((entry) => ({ taskType: entry.id, title: entry.title, gate: entry.gate, assignments: entry.assignments })),
    };
  }

  async proposeBoard(caller: McpCaller, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    this.requireCoordinator(caller);
    const requirements = normalizeRequirements(args.requirements);
    const rationale = text(args.rationale, "rationale", { required: false });
    const board = this.state.boards[caller.branch.id];
    for (const proposal of Object.values(this.state.proposals)) {
      if (proposal.branchId === caller.branch.id && proposal.status === "pending") proposal.status = "stale";
    }
    const proposal: BoardProposal = {
      id: newId("proposal"),
      branchId: caller.branch.id,
      baseRevision: board.revision,
      requirements,
      rationale,
      status: "pending",
      createdAt: this.clock(),
      decidedAt: null,
    };
    this.state.proposals[proposal.id] = proposal;
    this.recordEvent(caller.branch.id, "pm", "proposal_created", `PM proposed ${requirements.length} requirements for PO review.`);
    this.save();
    this.attention(caller.branch, "Board proposal", "The PM proposed requirements changes for your review.");
    return { proposalId: proposal.id, status: "pending", note: "Only the PO can accept it. You will be told the outcome." };
  }

  async createTask(caller: McpCaller, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    this.requireCoordinator(caller);
    const team = teamOf(caller.branch.runtime);
    const entry = taskTypeOf(team, text(args.taskType, "taskType", { max: 100 }));
    if (!entry) {
      throw new WorkflowError(400, "unknown_task_type", `taskType must be one of ${team.taskTypes.map((row) => row.id).join(", ")}`);
    }
    const branchId = caller.branch.id;
    const dependsOn = stringList(args.dependsOn, "dependsOn").map((dependency) => this.task(branchId, dependency).id);
    const resources = stringList(args.resources, "resources");
    for (const resource of resources) {
      const holder = Object.values(this.state.tasks).find(
        (task) => task.branchId === branchId && ACTIVE_TASK_STATUSES.includes(task.status) && task.resources.includes(resource),
      );
      if (holder) throw new WorkflowError(409, "resource_owned", `Resource ${resource} is owned by task ${holder.id} (${holder.title}).`);
    }
    const acceptanceCriteria = stringList(args.acceptanceCriteria, "acceptanceCriteria", { required: true });
    if (acceptanceCriteria.length === 0) throw new WorkflowError(400, "invalid_input", "At least one acceptance criterion is required.");
    const evidenceRequired = stringList(args.evidenceRequired, "evidenceRequired", { required: true });
    if (evidenceRequired.length === 0) throw new WorkflowError(400, "invalid_input", "State the evidence required for completion.");
    const now = this.clock();
    const gate = entry.gate && caller.branch.gates[entry.gate] ? entry.gate : null;
    const task: WorkflowTask = {
      id: newId("task"),
      branchId,
      taskType: entry.id,
      title: text(args.title, "title", { max: 300 }),
      deliverable: text(args.deliverable, "deliverable"),
      acceptanceCriteria,
      responsible: responsibleOf(entry),
      accountable: accountableOf(entry),
      dependsOn,
      resources,
      gate,
      evidenceRequired,
      status: "pending",
      evidence: [],
      assignedSessionIds: [],
      createdAt: now,
      updatedAt: now,
    };
    // Where the PO is responsible the task blocks on the human.
    if (task.responsible.includes("po")) task.status = "blocked";
    this.state.tasks[task.id] = task;
    this.recordEvent(branchId, "pm", "task_created", `Task "${task.title}" (${entry.title}); R: ${task.responsible.map((role) => this.roleName(branchId, role)).join(", ")}; A: ${this.roleName(branchId, task.accountable)}.`, task.id);
    this.save();
    if (task.status === "blocked") this.attention(caller.branch, "Task needs the PO", task.title);
    return { task };
  }

  private unmetDependencies(task: WorkflowTask): WorkflowTask[] {
    return task.dependsOn.map((id) => this.state.tasks[id]).filter((dependency) => dependency && dependency.status !== "done");
  }

  async dispatchTask(caller: McpCaller, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    this.requireCoordinator(caller);
    const branch = caller.branch;
    const task = this.task(branch.id, args.taskId);
    if (!ACTIVE_TASK_STATUSES.includes(task.status)) throw new WorkflowError(409, "task_closed", `Task is ${task.status}.`);
    // Waiting blocks dependent tasks only: refuse dispatch until dependencies (and their gates) are done.
    const unmet = this.unmetDependencies(task);
    if (unmet.length) {
      throw new WorkflowError(409, "dependencies_unmet", `Blocked by: ${unmet.map((dependency) => `${dependency.id} (${dependency.status})`).join(", ")}`);
    }
    const brief = text(args.brief, "brief", { required: false });
    const temporary = args.temporary === true;
    const team = teamOf(branch.runtime);
    const agentRoles = task.responsible.filter((role) => role !== PO_ROLE && hasRole(team, role));
    if (agentRoles.length === 0) {
      throw new WorkflowError(409, "human_responsible", "The PO is responsible for this task; it waits on the human.");
    }
    const delivered: string[] = [];
    for (const role of agentRoles) {
      const link = temporary
        ? await this.ensureRoleSession(branch, role, undefined, { temporaryTask: task })
        : await this.ensureRoleSession(branch, role);
      await this.relay(link.sessionId, this.taskBrief(task, role, brief));
      if (!task.assignedSessionIds.includes(link.sessionId)) task.assignedSessionIds.push(link.sessionId);
      delivered.push(this.roleName(caller.branch.id, role));
    }
    task.status = "in_progress";
    task.updatedAt = this.clock();
    this.recordEvent(branch.id, "pm", "task_dispatched", `Dispatched "${task.title}" to ${delivered.join(", ")}.`, task.id);
    this.save();
    return { dispatchedTo: delivered, status: task.status };
  }

  private taskBrief(task: WorkflowTask, role: AgentRoleId, brief: string): string {
    const row = taskTypeOf(teamOf(this.state.branches[task.branchId]?.runtime), task.taskType);
    const consulted = (row ? consultedOf(row) : []).filter((entry) => entry !== role);
    return [
      `[Office task ${task.id}] ${task.title}`,
      `Your role on it: ${task.responsible.includes(role) ? "Responsible" : "Accountable"}${task.accountable === role ? " and Accountable" : ""}.`,
      `Deliverable: ${task.deliverable}`,
      "Acceptance criteria:",
      ...task.acceptanceCriteria.map((criterion) => `- ${criterion}`),
      "Evidence required for completion:",
      ...task.evidenceRequired.map((evidence) => `- ${evidence}`),
      task.gate ? `Human gate: ${GATE_TITLES[task.gate]}.` : "No human gate.",
      consulted.length ? `Consult: ${consulted.map((role) => this.roleName(task.branchId, role)).join(", ")} (through the PM).` : "",
      brief ? `PM notes: ${brief}` : "",
      "Report progress with office_task_update and submit evidence when done.",
    ]
      .filter(Boolean)
      .join("\n");
  }

  async updateTask(caller: McpCaller, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    const branch = caller.branch;
    const task = this.task(branch.id, args.taskId);
    const role = caller.link.role;
    if (!task.responsible.includes(role) && task.accountable !== role && role !== COORDINATOR_ROLE) {
      throw new WorkflowError(403, "not_assigned", "You are not responsible or accountable for this task.");
    }
    if (!ACTIVE_TASK_STATUSES.includes(task.status)) throw new WorkflowError(409, "task_closed", `Task is ${task.status}.`);
    const status = args.status;
    if (status !== "in_progress" && status !== "blocked" && status !== "submitted") {
      throw new WorkflowError(400, "invalid_status", "status must be in_progress, blocked, or submitted");
    }
    const summary = text(args.summary, "summary");
    const references = stringList(args.references, "references");
    task.evidence.push({ at: this.clock(), role, summary, references });
    if (task.status !== "awaiting_gate") task.status = status;
    task.updatedAt = this.clock();
    this.recordEvent(branch.id, role, `task_${status}`, `${this.roleName(caller.branch.id, role)}: ${summary}`, task.id);
    this.save();
    if (status === "submitted" && task.accountable !== role) {
      const delivered = await this.relayToRole(
        branch,
        task.accountable,
        `[Office] ${this.roleName(caller.branch.id, role)} submitted task ${task.id} "${task.title}": ${summary}\nReview it against the acceptance criteria and complete it with office_task_complete (or request a gate first).`,
      );
      if (!delivered && task.accountable === "po") this.attention(branch, "Task ready for the PO", task.title);
    }
    if (status === "blocked" && role !== COORDINATOR_ROLE) {
      await this.relayToRole(branch, COORDINATOR_ROLE, `[Office] ${this.roleName(caller.branch.id, role)} is blocked on task ${task.id} "${task.title}": ${summary}`);
    }
    return { taskId: task.id, status: task.status };
  }

  async completeTask(caller: McpCaller, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    const branch = caller.branch;
    const task = this.task(branch.id, args.taskId);
    if (task.accountable !== caller.link.role) {
      throw new WorkflowError(403, "not_accountable", `Only the accountable role (${this.roleName(caller.branch.id, task.accountable)}) completes this task.`);
    }
    if (task.status === "done") return { taskId: task.id, status: "done" };
    if (!ACTIVE_TASK_STATUSES.includes(task.status)) throw new WorkflowError(409, "task_closed", `Task is ${task.status}.`);
    if (task.gate && branch.gates[task.gate] && !this.hasApprovedGate(task)) {
      throw new WorkflowError(409, "gate_required", `Request the ${GATE_TITLES[task.gate]} gate with office_gate_request and wait for PO approval.`);
    }
    const summary = text(args.summary, "summary");
    const references = stringList(args.references, "references");
    if (task.evidence.length === 0 && references.length === 0) {
      throw new WorkflowError(409, "evidence_required", `Provide evidence: ${task.evidenceRequired.join("; ")}`);
    }
    task.evidence.push({ at: this.clock(), role: caller.link.role, summary, references });
    await this.finishTask(branch, task, caller.link.role);
    return { taskId: task.id, status: task.status };
  }

  private async finishTask(branch: ThemeBranch, task: WorkflowTask, actor: RoleId): Promise<void> {
    task.status = "done";
    task.updatedAt = this.clock();
    this.recordEvent(branch.id, actor, "task_done", `Task "${task.title}" completed.`, task.id);
    const ready = Object.values(this.state.tasks).filter(
      (other) => other.branchId === branch.id && other.status === "pending" && other.dependsOn.includes(task.id) && this.unmetDependencies(other).length === 0,
    );
    // Temporary workers end with their task.
    for (const link of this.liveLinks(branch.id)) {
      if (link.temporary && link.taskId === task.id) link.retiredAt = this.clock();
    }
    this.save();
    // An operation approval ends when its task completes.
    if (task.gate && RUNTIME_GATE_PATTERNS[task.gate]) await this.refreshGatePolicies(branch);
    if (actor !== COORDINATOR_ROLE) {
      await this.relayToRole(
        branch,
        COORDINATOR_ROLE,
        `[Office] Task ${task.id} "${task.title}" is done.${ready.length ? ` Now ready: ${ready.map((other) => `${other.id} "${other.title}"`).join(", ")}.` : ""}`,
      );
    }
  }

  async requestGate(caller: McpCaller, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    const branch = caller.branch;
    const gateType = args.gate as GateType;
    if (!(gateType in GATE_TITLES)) throw new WorkflowError(400, "invalid_gate", `gate must be one of ${Object.keys(GATE_TITLES).join(", ")}`);
    if (!branch.gates[gateType]) {
      throw new WorkflowError(409, "gate_disabled", `The ${GATE_TITLES[gateType]} gate is disabled for this project; no approval is needed.`);
    }
    const task = args.taskId ? this.task(branch.id, args.taskId) : null;
    if (task) {
      const role = caller.link.role;
      if (task.accountable !== role && !task.responsible.includes(role) && role !== COORDINATOR_ROLE) {
        throw new WorkflowError(403, "not_assigned", "You may only request gates for your own tasks.");
      }
    }
    const operation = text(args.operation, "operation", { required: false, max: 2000 }) || null;
    if (RUNTIME_GATE_PATTERNS[gateType] && !operation) {
      throw new WorkflowError(400, "operation_required", `The ${GATE_TITLES[gateType]} gate needs the exact command it authorizes.`);
    }
    if (operation && RUNTIME_GATE_PATTERNS[gateType] && !new RegExp(RUNTIME_GATE_PATTERNS[gateType]!).test(operation)) {
      throw new WorkflowError(400, "operation_not_gated", "That command is not a gated operation; no approval is needed for it.");
    }
    const board = this.state.boards[branch.id];
    const artifactKind = (args.artifactKind ?? (gateType === "requirements" ? "board" : "text")) as GateArtifact["kind"];
    if (!["board", "file", "commit", "pull_request", "text"].includes(artifactKind)) {
      throw new WorkflowError(400, "invalid_artifact", "artifactKind must be board, file, commit, pull_request, or text");
    }
    let artifact: GateArtifact;
    if (artifactKind === "board") {
      artifact = { kind: "board", ref: `requirements board revision ${board.revision}`, digest: board.digest, boardRevision: board.revision };
    } else {
      const ref = text(args.artifactRef, "artifactRef", { max: 2000 });
      const supplied = text(args.artifactDigest, "artifactDigest", { required: false, max: 200 });
      artifact = {
        kind: artifactKind,
        ref,
        digest: supplied || sha256(`${artifactKind}:${ref}:${text(args.summary, "summary")}`),
        boardRevision: board.revision,
      };
    }
    const now = this.clock();
    // A new request for the same task and gate supersedes the open one.
    for (const other of Object.values(this.state.gates)) {
      if (other.branchId === branch.id && other.gate === gateType && other.taskId === (task?.id ?? null) && other.status === "pending") {
        other.status = "superseded";
        other.statusReason = "A newer request replaced it.";
      }
    }
    const request: GateRequest = {
      id: newId("gate"),
      branchId: branch.id,
      taskId: task?.id ?? null,
      gate: gateType,
      requestedBy: caller.link.role,
      requestedBySessionId: caller.link.sessionId,
      summary: text(args.summary, "summary"),
      operation,
      targetEnvironment: text(args.targetEnvironment, "targetEnvironment", { required: false, max: 200 }) || null,
      artifact,
      gatePolicyRevision: branch.gatePolicyRevision,
      status: "pending",
      createdAt: now,
      decidedAt: null,
      feedback: null,
      statusReason: null,
    };
    this.state.gates[request.id] = request;
    if (task && task.status !== "done") {
      task.status = "awaiting_gate";
      task.updatedAt = now;
    }
    this.recordEvent(branch.id, caller.link.role, "gate_requested", `${this.roleName(caller.branch.id, caller.link.role)} requested the ${GATE_TITLES[gateType]} gate: ${request.summary}`, request.taskId);
    this.save();
    this.attention(branch, `${GATE_TITLES[gateType]} approval needed`, request.summary);
    return { gateId: request.id, status: "pending", note: "Wait for the PO's decision; you will be told the outcome. Continue independent work meanwhile." };
  }

  async messageRole(caller: McpCaller, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    const target = args.role;
    const roles = teamOf(caller.branch.runtime).roles.map((role) => role.id);
    if (!hasRole(teamOf(caller.branch.runtime), target)) throw new WorkflowError(400, "invalid_role", `role must be one of ${roles.join(", ")}`);
    if (target === caller.link.role) throw new WorkflowError(400, "invalid_role", "You cannot message yourself.");
    // Specialists coordinate through the PM; only the PM addresses specialists directly.
    if (caller.link.role !== COORDINATOR_ROLE && target !== COORDINATOR_ROLE) {
      throw new WorkflowError(403, "route_through_coordinator", "Message the PM; the PM coordinates other roles.");
    }
    const body = text(args.text, "text");
    const link = caller.link.role === COORDINATOR_ROLE ? await this.ensureRoleSession(caller.branch, target) : this.persistentLink(caller.branch.id, target);
    if (!link) throw new WorkflowError(409, "role_not_running", `${this.roleName(caller.branch.id, target)} is not running.`);
    await this.relay(link.sessionId, `[From ${this.roleName(caller.branch.id, caller.link.role)} via the office] ${body}`);
    this.recordEvent(caller.branch.id, caller.link.role, "message", `${this.roleName(caller.branch.id, caller.link.role)} → ${this.roleName(caller.branch.id, target)}: ${body.slice(0, 200)}`);
    this.save();
    return { delivered: true };
  }

  async recordDecision(caller: McpCaller, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    this.requireCoordinator(caller);
    const team = teamOf(caller.branch.runtime);
    const affected = stringList(args.affectedRoles, "affectedRoles").filter((role) => hasRole(team, role));
    const decision = {
      id: newId("decision"),
      branchId: caller.branch.id,
      at: this.clock(),
      summary: text(args.summary, "summary"),
      affectedRoles: affected,
      requestedBy: (hasRole(team, args.requestedBy) || args.requestedBy === PO_ROLE ? args.requestedBy : "pm") as RoleId,
    };
    this.state.decisions[decision.id] = decision;
    this.recordEvent(caller.branch.id, "pm", "decision", `Coordinator decision: ${decision.summary}`);
    this.save();
    for (const role of affected) {
      await this.relayToRole(caller.branch, role, `[Office] Coordinator decision ${decision.id}: ${decision.summary}`);
    }
    return { decisionId: decision.id };
  }

  private currentInterview(branchId: string): InterviewRound | null {
    const rounds = Object.values(this.state.interviewRounds).filter((round) => round.branchId === branchId && round.endedAt === null);
    return rounds.sort((left, right) => right.startedAt - left.startedAt)[0] ?? null;
  }

  private roundQuestions(interviewId: string): InterviewQuestion[] {
    return Object.values(this.state.interviews)
      .filter((question) => question.interviewId === interviewId)
      .sort((left, right) => left.addedAt - right.addedAt);
  }

  private openQuestion(branchId: string): InterviewQuestion | undefined {
    return Object.values(this.state.interviews).find((question) => question.branchId === branchId && question.status === "asked");
  }

  /** The PM may also interview without a PO kickoff; that starts an untitled interview. */
  private ensureInterview(branchId: string): InterviewRound {
    const current = this.currentInterview(branchId);
    if (current) return current;
    const round: InterviewRound = { id: newId("interview"), branchId, topic: "", startedAt: this.clock(), endedAt: null };
    this.state.interviewRounds[round.id] = round;
    return round;
  }

  /** The first questions of an interview are its agenda; later ones are follow-ups. */
  private nextOrigin(round: InterviewRound): InterviewQuestion["origin"] {
    return this.roundQuestions(round.id).length === 0 ? "plan" : "added";
  }

  private addQuestion(round: InterviewRound, questionText: string, origin: InterviewQuestion["origin"]): InterviewQuestion {
    const question: InterviewQuestion = {
      id: newId("question"),
      branchId: round.branchId,
      interviewId: round.id,
      question: questionText,
      status: "planned",
      origin,
      addedAt: this.clock(),
      askedAt: null,
      answer: null,
      answeredAt: null,
      skipReason: null,
    };
    this.state.interviews[question.id] = question;
    return question;
  }

  /** Interview agenda for the PM's board snapshot. */
  private interviewAgenda(branchId: string): Record<string, unknown> | null {
    const round = this.currentInterview(branchId);
    if (!round) return null;
    return {
      interviewId: round.id,
      topic: round.topic,
      questions: this.roundQuestions(round.id).map(({ id, question, status, origin }) => ({ id, question, status, origin })),
    };
  }

  planInterview(caller: McpCaller, args: Record<string, unknown>): Record<string, unknown> {
    this.requireCoordinator(caller);
    const questions = stringList(args.questions, "questions");
    if (questions.length === 0) throw new WorkflowError(400, "invalid_input", "questions must list at least one question");
    const round = this.ensureInterview(caller.branch.id);
    const existing = new Set(this.roundQuestions(round.id).filter((entry) => entry.status !== "skipped").map((entry) => entry.question));
    const origin = this.nextOrigin(round);
    const added = [...new Set(questions)].filter((entry) => !existing.has(entry)).map((entry) => this.addQuestion(round, entry.slice(0, 2000), origin));
    if (added.length) {
      this.recordEvent(caller.branch.id, "pm", "interview_planned", `PM planned ${added.length} interview question${added.length === 1 ? "" : "s"}.`);
    }
    this.save();
    return { interviewId: round.id, added: added.map(({ id, question, origin }) => ({ id, question, origin })) };
  }

  async askInterview(caller: McpCaller, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    this.requireCoordinator(caller);
    const open = this.openQuestion(caller.branch.id);
    if (open) throw new WorkflowError(409, "question_pending", `Wait for the PO to answer: ${open.question}`);
    const round = this.ensureInterview(caller.branch.id);
    const planned = this.roundQuestions(round.id).filter((entry) => entry.status === "planned");
    let question: InterviewQuestion | undefined;
    if (args.questionId) {
      question = planned.find((entry) => entry.id === args.questionId);
      if (!question) throw new WorkflowError(404, "question_not_found", "No planned question with that id in the current interview.");
    } else {
      const questionText = text(args.question, "question", { max: 2000 });
      question = planned.find((entry) => entry.question === questionText) ?? this.addQuestion(round, questionText, this.nextOrigin(round));
    }
    question.status = "asked";
    question.askedAt = this.clock();
    this.recordEvent(caller.branch.id, "pm", "interview_question", `PM asks: ${question.question}`);
    this.save();
    this.attention(caller.branch, "PM interview question", question.question);
    return { questionId: question.id, note: "The answer will arrive as a message from the PO." };
  }

  skipInterview(caller: McpCaller, args: Record<string, unknown>): Record<string, unknown> {
    this.requireCoordinator(caller);
    const question = this.state.interviews[String(args.questionId ?? "")];
    if (!question || question.branchId !== caller.branch.id) throw new WorkflowError(404, "question_not_found", "No such question");
    if (question.status !== "planned") throw new WorkflowError(409, "not_planned", "Only a question that has not been asked can be skipped.");
    question.status = "skipped";
    question.skipReason = text(args.reason, "reason", { max: 1000 });
    this.recordEvent(caller.branch.id, "pm", "interview_skipped", `PM skipped: ${question.question} (${question.skipReason})`);
    this.save();
    return { questionId: question.id, status: question.status };
  }

  postUpdate(caller: McpCaller, args: Record<string, unknown>): Record<string, unknown> {
    const taskId = args.taskId ? this.task(caller.branch.id, args.taskId).id : null;
    const event = this.recordEvent(caller.branch.id, caller.link.role, "update", `${this.roleName(caller.branch.id, caller.link.role)}: ${text(args.summary, "summary", { max: 2000 })}`, taskId);
    this.save();
    return { eventId: event.id };
  }
}

function publicLink(link: StoredRoleSession): RoleSessionLink {
  const { mcpToken: _token, gatePolicies: _policies, ...rest } = link;
  return rest;
}

const MAX_CUSTOM_INSTRUCTIONS = 8_000;

function isEffort(value: unknown): value is string {
  return typeof value === "string" && (EFFORT_ORDER as readonly string[]).includes(value);
}

/** Omnigent policy name of an office gate; each replacement gets a new version. */
export function gatePolicyName(gate: GateType, version?: number): string {
  return version === undefined ? `office_${gate}_gate` : `office_${gate}_gate_v${version}`;
}

/** Distinct session ids from a request body; at least one. */
function requestedSessionIds(input: unknown, emptyMessage: string): string[] {
  const sessionIds = Array.isArray(input) ? [...new Set(input.filter((id): id is string => typeof id === "string"))] : [];
  if (sessionIds.length === 0) throw new WorkflowError(400, "no_sessions", emptyMessage);
  return sessionIds;
}

/**
 * A room's runtime from untrusted input. `toolsFor` lists a role's office tools, so per-role
 * tool settings can be checked; without it they are dropped.
 */
export function normalizeRuntime(value: unknown, toolsFor?: (role: string) => string[]): BranchRuntime {
  if (!value || typeof value !== "object") throw new WorkflowError(400, "invalid_runtime", "runtime is required");
  const record = value as Record<string, unknown>;
  let team = sdlcTeam();
  if (record.team !== undefined) {
    const parsed = parseTeam(record.team);
    if ("error" in parsed) throw new WorkflowError(400, "invalid_team", parsed.error);
    team = parsed.team;
  }
  const isAgentRole = (role: unknown): role is string => hasRole(team, role);
  const harness = text(record.harness, "runtime.harness", { required: false, max: 100 });
  const optional = (field: string) => text(record[field], `runtime.${field}`, { required: false, max: 1000 }) || undefined;
  const runtime: BranchRuntime = {};
  if (harness) runtime.harness = harness;
  for (const field of ["model", "hostId", "workspace", "runnerId", "teamTemplateId"] as const) {
    const entry = optional(field);
    if (entry) runtime[field] = entry;
  }
  if (record.roleHarnesses && typeof record.roleHarnesses === "object") {
    const roleHarnesses: Partial<Record<AgentRoleId, string>> = {};
    for (const [role, value] of Object.entries(record.roleHarnesses as Record<string, unknown>)) {
      if (isAgentRole(role) && typeof value === "string" && value.trim()) roleHarnesses[role] = value.trim().slice(0, 100);
    }
    if (Object.keys(roleHarnesses).length) runtime.roleHarnesses = roleHarnesses;
  }
  if (record.roleModels && typeof record.roleModels === "object") {
    const roleModels: Partial<Record<AgentRoleId, string>> = {};
    for (const [role, model] of Object.entries(record.roleModels as Record<string, unknown>)) {
      if (isAgentRole(role) && typeof model === "string" && model.trim()) roleModels[role] = model.trim().slice(0, 200);
    }
    if (Object.keys(roleModels).length) runtime.roleModels = roleModels;
  }
  if (record.roleCustomInstructions && typeof record.roleCustomInstructions === "object") {
    const custom: Partial<Record<AgentRoleId, string>> = {};
    for (const [role, value] of Object.entries(record.roleCustomInstructions as Record<string, unknown>)) {
      if (isAgentRole(role) && typeof value === "string" && value.trim()) custom[role] = value.trim().slice(0, MAX_CUSTOM_INSTRUCTIONS);
    }
    if (Object.keys(custom).length) runtime.roleCustomInstructions = custom;
  }
  if (record.roleEfforts && typeof record.roleEfforts === "object") {
    const roleEfforts: Partial<Record<AgentRoleId, string>> = {};
    for (const [role, effort] of Object.entries(record.roleEfforts as Record<string, unknown>)) {
      if (isAgentRole(role) && isEffort(effort)) roleEfforts[role] = effort;
    }
    if (Object.keys(roleEfforts).length) runtime.roleEfforts = roleEfforts;
  }
  if (!sameTeam(team, sdlcTeam())) runtime.team = team;
  if (toolsFor && record.roleToolSettings && typeof record.roleToolSettings === "object") {
    const settings: NonNullable<BranchRuntime["roleToolSettings"]> = {};
    for (const [role, entry] of Object.entries(record.roleToolSettings as Record<string, unknown>)) {
      if (!isAgentRole(role)) continue;
      const parsed = parseRoleToolSettings(entry, role, toolsFor(role));
      if ("error" in parsed) throw new WorkflowError(400, "invalid_tool_settings", `${teamRoleTitle(team, role)}: ${parsed.error}`);
      if (Object.keys(parsed.settings).length) settings[role] = parsed.settings;
    }
    if (Object.keys(settings).length) runtime.roleToolSettings = settings;
  }
  if (!runtime.hostId && !runtime.runnerId) {
    throw new WorkflowError(400, "invalid_runtime", "Choose a host (and workspace) for the role sessions.");
  }
  if (runtime.hostId && !runtime.workspace) {
    throw new WorkflowError(400, "invalid_runtime", "Choose a workspace folder on the host for the role sessions.");
  }
  return runtime;
}
