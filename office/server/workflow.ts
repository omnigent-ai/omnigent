// Durable SDLC workflow orchestration. The office service owns the task
// graph, requirements board, and workflow gates; Omnigent owns sessions,
// execution, and runtime policy. Role sessions reach this service through
// the office MCP endpoint, and the PO through the office HTTP API.

import { createHash, randomBytes } from "node:crypto";
import {
  AGENT_ROLES,
  COORDINATOR_ROLE,
  DEFAULT_GATES,
  GATE_INVALIDATES,
  GATE_TITLES,
  RUNTIME_GATE_PATTERNS,
  SDLC_MATRIX,
  accountableRole,
  isAgentRole,
  matrixRow,
  responsibleRoles,
  roleTitle,
  rolesWith,
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
  Requirement,
  RequirementsBoard,
  RoleSessionLink,
  TaskStatus,
  ThemeBranch,
  WorkflowEvent,
  WorkflowState,
  WorkflowTask,
} from "../shared/workflowTypes.ts";
import { gatePolicyExpression, roleBundle } from "./bundle.ts";
import type { Notifier } from "./notifier.ts";
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
  addSessionPolicy(sessionId: string, name: string, expression: string): Promise<{ id: string }>;
  deleteSessionPolicy(sessionId: string, policyId: string): Promise<void>;
  listProjects(): Promise<{ id: string; name: string }[]>;
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

  callerForToken(token: string): McpCaller | null {
    if (!token) return null;
    const link = (this.state.roleSessions as StoredRoleSession[]).find((entry) => entry.mcpToken === token);
    if (!link || link.retiredAt !== null) return null;
    const branch = this.state.branches[link.branchId];
    return branch ? { link, branch } : null;
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
    const runtime = normalizeRuntime(input.runtime);
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

  private async projectName(projectId: string): Promise<string> {
    const projects = await this.omnigent.listProjects();
    return projects.find((entry) => entry.id === projectId)?.name ?? projectId;
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
    const name = projectName ?? (await this.projectName(branch.projectId));
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
      ? `${roleTitle(role)} · ${options.temporaryTask.title}`.slice(0, 200)
      : `${roleTitle(role)} · SDLC`;
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
    const created = await this.omnigent.createBundledSession(bundle, metadata);
    if (branch.runtime.runnerId) await this.omnigent.bindRunner(created.session_id, branch.runtime.runnerId);
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
      `${roleTitle(role)} session started${options.temporaryTask ? ` as a temporary worker for "${options.temporaryTask.title}"` : ""}.`,
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
      const created = await this.omnigent.addSessionPolicy(link.sessionId, `office_${gate}_gate_v${version}`, expression);
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
        this.recordEvent(branch.id, "office", "policy_update_failed", `Could not update gate policy on ${roleTitle(link.role)}: ${(error as Error).message}`);
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
    if (!isAgentRole(role)) return false;
    const link = this.persistentLink(branch.id, role);
    if (!link) return false;
    try {
      await this.relay(link.sessionId, message);
      return true;
    } catch (error) {
      this.recordEvent(branch.id, "office", "relay_failed", `Could not reach ${roleTitle(role)}: ${(error as Error).message}`);
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
    if (!isAgentRole(roleInput)) throw new WorkflowError(400, "invalid_role", "Unknown role");
    const role = roleInput;
    const reason = text(reasonInput, "reason", { required: false, max: 2000 }) || "PO requested a fresh session.";
    const previous = this.persistentLink(branchId, role);
    const next = await this.ensureRoleSession(branch, role, undefined, { handoffFrom: previous ?? undefined });
    if (previous) {
      previous.retiredAt = this.clock();
      await this.omnigent.interrupt(previous.sessionId).catch(() => undefined);
    }
    this.recordEvent(branchId, "office", "handoff", `${roleTitle(role)} rolled over to a new session (${reason}).`);
    this.save();
    await this.relay(next.sessionId, this.handoffBrief(branch, role, previous, reason));
    return this.view(branchId);
  }

  private handoffBrief(branch: ThemeBranch, role: AgentRoleId, previous: StoredRoleSession | undefined, reason: string): string {
    const tasks = Object.values(this.state.tasks).filter(
      (task) => task.branchId === branch.id && ACTIVE_TASK_STATUSES.includes(task.status) && (role === COORDINATOR_ROLE || task.responsible.includes(role) || task.accountable === role),
    );
    const lines = [
      `[Office handoff] You are taking over the ${roleTitle(role)} role${previous ? ` from session ${previous.sessionId}` : ""}. Reason: ${reason}`,
      `Requirements board revision ${this.state.boards[branch.id].revision}. Read the full state with office_board_get.`,
      tasks.length ? "Open tasks:" : "No open tasks.",
      ...tasks.map((task) => `- ${task.id} [${task.status}] ${task.title}`),
    ];
    return lines.join("\n");
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
      you: { role: caller.link.role, title: roleTitle(caller.link.role), sessionId: caller.link.sessionId },
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
      matrix: SDLC_MATRIX.map((entry) => ({ taskType: entry.id, title: entry.title, assignments: entry.assignments })),
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
    const entry = matrixRow(text(args.taskType, "taskType", { max: 100 }));
    if (!entry) {
      throw new WorkflowError(400, "unknown_task_type", `taskType must be one of ${SDLC_MATRIX.map((row) => row.id).join(", ")}`);
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
      responsible: responsibleRoles(entry),
      accountable: accountableRole(entry),
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
    this.recordEvent(branchId, "pm", "task_created", `Task "${task.title}" (${entry.title}); R: ${task.responsible.map(roleTitle).join(", ")}; A: ${roleTitle(task.accountable)}.`, task.id);
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
    const agentRoles = task.responsible.filter(isAgentRole);
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
      delivered.push(roleTitle(role));
    }
    task.status = "in_progress";
    task.updatedAt = this.clock();
    this.recordEvent(branch.id, "pm", "task_dispatched", `Dispatched "${task.title}" to ${delivered.join(", ")}.`, task.id);
    this.save();
    return { dispatchedTo: delivered, status: task.status };
  }

  private taskBrief(task: WorkflowTask, role: AgentRoleId, brief: string): string {
    const consulted = rolesWith(matrixRow(task.taskType)!, "C").filter((entry) => entry !== role);
    return [
      `[Office task ${task.id}] ${task.title}`,
      `Your role on it: ${task.responsible.includes(role) ? "Responsible" : "Accountable"}${task.accountable === role ? " and Accountable" : ""}.`,
      `Deliverable: ${task.deliverable}`,
      "Acceptance criteria:",
      ...task.acceptanceCriteria.map((criterion) => `- ${criterion}`),
      "Evidence required for completion:",
      ...task.evidenceRequired.map((evidence) => `- ${evidence}`),
      task.gate ? `Human gate: ${GATE_TITLES[task.gate]}.` : "No human gate.",
      consulted.length ? `Consult: ${consulted.map(roleTitle).join(", ")} (through the PM).` : "",
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
    this.recordEvent(branch.id, role, `task_${status}`, `${roleTitle(role)}: ${summary}`, task.id);
    this.save();
    if (status === "submitted" && task.accountable !== role) {
      const delivered = await this.relayToRole(
        branch,
        task.accountable,
        `[Office] ${roleTitle(role)} submitted task ${task.id} "${task.title}": ${summary}\nReview it against the acceptance criteria and complete it with office_task_complete (or request a gate first).`,
      );
      if (!delivered && task.accountable === "po") this.attention(branch, "Task ready for the PO", task.title);
    }
    if (status === "blocked" && role !== COORDINATOR_ROLE) {
      await this.relayToRole(branch, COORDINATOR_ROLE, `[Office] ${roleTitle(role)} is blocked on task ${task.id} "${task.title}": ${summary}`);
    }
    return { taskId: task.id, status: task.status };
  }

  async completeTask(caller: McpCaller, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    const branch = caller.branch;
    const task = this.task(branch.id, args.taskId);
    if (task.accountable !== caller.link.role) {
      throw new WorkflowError(403, "not_accountable", `Only the accountable role (${roleTitle(task.accountable)}) completes this task.`);
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
    this.recordEvent(branch.id, caller.link.role, "gate_requested", `${roleTitle(caller.link.role)} requested the ${GATE_TITLES[gateType]} gate: ${request.summary}`, request.taskId);
    this.save();
    this.attention(branch, `${GATE_TITLES[gateType]} approval needed`, request.summary);
    return { gateId: request.id, status: "pending", note: "Wait for the PO's decision; you will be told the outcome. Continue independent work meanwhile." };
  }

  async messageRole(caller: McpCaller, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    const target = args.role;
    if (!isAgentRole(target)) throw new WorkflowError(400, "invalid_role", `role must be one of ${AGENT_ROLES.join(", ")}`);
    if (target === caller.link.role) throw new WorkflowError(400, "invalid_role", "You cannot message yourself.");
    // Specialists coordinate through the PM; only the PM addresses specialists directly.
    if (caller.link.role !== COORDINATOR_ROLE && target !== COORDINATOR_ROLE) {
      throw new WorkflowError(403, "route_through_coordinator", "Message the PM; the PM coordinates other roles.");
    }
    const body = text(args.text, "text");
    const link = caller.link.role === COORDINATOR_ROLE ? await this.ensureRoleSession(caller.branch, target) : this.persistentLink(caller.branch.id, target);
    if (!link) throw new WorkflowError(409, "role_not_running", `${roleTitle(target)} is not running.`);
    await this.relay(link.sessionId, `[From ${roleTitle(caller.link.role)} via the office] ${body}`);
    this.recordEvent(caller.branch.id, caller.link.role, "message", `${roleTitle(caller.link.role)} → ${roleTitle(target)}: ${body.slice(0, 200)}`);
    this.save();
    return { delivered: true };
  }

  async recordDecision(caller: McpCaller, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    this.requireCoordinator(caller);
    const affected = stringList(args.affectedRoles, "affectedRoles").filter(isAgentRole);
    const decision = {
      id: newId("decision"),
      branchId: caller.branch.id,
      at: this.clock(),
      summary: text(args.summary, "summary"),
      affectedRoles: affected,
      requestedBy: (isAgentRole(args.requestedBy) || args.requestedBy === "po" ? args.requestedBy : "pm") as RoleId,
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
    const event = this.recordEvent(caller.branch.id, caller.link.role, "update", `${roleTitle(caller.link.role)}: ${text(args.summary, "summary", { max: 2000 })}`, taskId);
    this.save();
    return { eventId: event.id };
  }
}

function publicLink(link: StoredRoleSession): RoleSessionLink {
  const { mcpToken: _token, gatePolicies: _policies, ...rest } = link;
  return rest;
}

export function normalizeRuntime(value: unknown): BranchRuntime {
  if (!value || typeof value !== "object") throw new WorkflowError(400, "invalid_runtime", "runtime is required");
  const record = value as Record<string, unknown>;
  const harness = text(record.harness, "runtime.harness", { max: 100 });
  const optional = (field: string) => text(record[field], `runtime.${field}`, { required: false, max: 1000 }) || undefined;
  const runtime: BranchRuntime = { harness };
  for (const field of ["model", "hostId", "workspace", "runnerId"] as const) {
    const entry = optional(field);
    if (entry) runtime[field] = entry;
  }
  if (record.roleModels && typeof record.roleModels === "object") {
    const roleModels: Partial<Record<AgentRoleId, string>> = {};
    for (const [role, model] of Object.entries(record.roleModels as Record<string, unknown>)) {
      if (isAgentRole(role) && typeof model === "string" && model.trim()) roleModels[role] = model.trim().slice(0, 200);
    }
    if (Object.keys(roleModels).length) runtime.roleModels = roleModels;
  }
  if (!runtime.hostId && !runtime.runnerId) {
    throw new WorkflowError(400, "invalid_runtime", "Choose a host (and workspace) for the role sessions.");
  }
  return runtime;
}
