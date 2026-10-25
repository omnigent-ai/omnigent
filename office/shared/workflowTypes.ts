// Durable workflow records owned by the office service. Branch identity is
// part of every record so additional theme branches need no migration.

import type { AgentRoleId, GateType, RoleId } from "./sdlcTheme.ts";
import type { RoleHealth } from "./troubleshoot.ts";

export type ThemeId = "sdlc";
export type BranchStatus = "active" | "background" | "paused" | "archived";

/** How the office launches role sessions on the connected Omnigent. */
export interface BranchRuntime {
  /** Overrides every role's harness; omit to use the per-role defaults. */
  harness?: string;
  /** Per-role harness overrides. */
  roleHarnesses?: Partial<Record<AgentRoleId, string>>;
  model?: string;
  /** Per-role model overrides, e.g. a stronger model for the Architect. */
  roleModels?: Partial<Record<AgentRoleId, string>>;
  hostId?: string;
  workspace?: string;
  /** Bind to an already running runner instead of launching on a host. */
  runnerId?: string;
}

export interface ThemeBranch {
  id: string;
  projectId: string;
  theme: ThemeId;
  /** Digest of the theme definition the branch was created with. */
  themeRevision: string;
  status: BranchStatus;
  createdAt: number;
  runtime: BranchRuntime;
  gates: Record<GateType, boolean>;
  /** Revision of the gate configuration and runtime patterns, bound into every gate request. */
  gatePolicyRevision: string;
}

export interface RoleSessionLink {
  branchId: string;
  role: AgentRoleId;
  sessionId: string;
  /** Temporary workers serve one task; persistent ones are the role's conversation. */
  temporary: boolean;
  taskId: string | null;
  createdAt: number;
  /** Set when the role rolled over to a new session through a recorded handoff. */
  retiredAt: number | null;
  handoffFromSessionId: string | null;
}

export interface RoleStartFailure {
  branchId: string;
  role: AgentRoleId;
  harness: string;
  at: number;
  code: string;
  message: string;
}

export interface Requirement {
  id: string;
  title: string;
  description: string;
  acceptanceCriteria: string[];
}

export interface RequirementsBoard {
  branchId: string;
  revision: number;
  digest: string;
  requirements: Requirement[];
  updatedAt: number;
  updatedBy: RoleId;
}

export type ProposalStatus = "pending" | "accepted" | "rejected" | "stale";

export interface BoardProposal {
  id: string;
  branchId: string;
  baseRevision: number;
  requirements: Requirement[];
  rationale: string;
  status: ProposalStatus;
  createdAt: number;
  decidedAt: number | null;
}

export type TaskStatus =
  | "pending"
  | "in_progress"
  | "blocked"
  | "submitted"
  | "awaiting_gate"
  | "done"
  | "cancelled";

export interface TaskEvidence {
  at: number;
  role: RoleId;
  summary: string;
  references: string[];
}

export interface WorkflowTask {
  id: string;
  branchId: string;
  taskType: string;
  title: string;
  deliverable: string;
  acceptanceCriteria: string[];
  responsible: RoleId[];
  accountable: RoleId;
  dependsOn: string[];
  resources: string[];
  gate: GateType | null;
  evidenceRequired: string[];
  status: TaskStatus;
  evidence: TaskEvidence[];
  /** Sessions the task was dispatched to. */
  assignedSessionIds: string[];
  createdAt: number;
  updatedAt: number;
}

export type GateStatus = "pending" | "approved" | "changes_requested" | "stale" | "superseded" | "cancelled";

export interface GateArtifact {
  kind: "board" | "file" | "commit" | "pull_request" | "text";
  ref: string;
  digest: string;
  /** Requirements revision the artifact was produced against, when known. */
  boardRevision: number | null;
}

export interface GateRequest {
  id: string;
  branchId: string;
  taskId: string | null;
  gate: GateType;
  requestedBy: RoleId;
  requestedBySessionId: string | null;
  summary: string;
  /** Exact operation the approval authorizes, e.g. a shell command; null for document gates. */
  operation: string | null;
  targetEnvironment: string | null;
  artifact: GateArtifact;
  gatePolicyRevision: string;
  status: GateStatus;
  createdAt: number;
  decidedAt: number | null;
  feedback: string | null;
  statusReason: string | null;
}

export interface WorkflowEvent {
  id: string;
  branchId: string;
  at: number;
  actor: RoleId | "office";
  kind: string;
  summary: string;
  taskId: string | null;
}

/** One PO interview: the PM's agenda of questions on a topic. */
export interface InterviewRound {
  id: string;
  branchId: string;
  topic: string;
  startedAt: number;
  /** Set when a later interview replaces this one. */
  endedAt: number | null;
}

/**
 * `planned`: on the agenda, not yet asked. `asked`: waiting for the PO.
 * `origin: "added"` marks questions added after the agenda was first set.
 */
export type InterviewQuestionStatus = "planned" | "asked" | "answered" | "skipped";

export interface InterviewQuestion {
  id: string;
  branchId: string;
  interviewId: string;
  question: string;
  status: InterviewQuestionStatus;
  origin: "plan" | "added";
  addedAt: number;
  askedAt: number | null;
  answer: string | null;
  answeredAt: number | null;
  skipReason: string | null;
}

export interface CoordinatorDecision {
  id: string;
  branchId: string;
  at: number;
  summary: string;
  affectedRoles: RoleId[];
  requestedBy: RoleId;
}

export interface WorkflowState {
  branches: Record<string, ThemeBranch>;
  roleSessions: RoleSessionLink[];
  boards: Record<string, RequirementsBoard>;
  proposals: Record<string, BoardProposal>;
  tasks: Record<string, WorkflowTask>;
  gates: Record<string, GateRequest>;
  events: WorkflowEvent[];
  interviews: Record<string, InterviewQuestion>;
  interviewRounds: Record<string, InterviewRound>;
  decisions: Record<string, CoordinatorDecision>;
  /** Item ids of messages the office relayed for agents; never human interactions. */
  relayedItemIds: string[];
  /** Latest failure to start each role, keyed `${branchId}:${role}`; cleared when it starts. */
  roleStartFailures: Record<string, RoleStartFailure>;
}

export function emptyWorkflowState(): WorkflowState {
  return {
    branches: {},
    roleSessions: [],
    boards: {},
    proposals: {},
    tasks: {},
    gates: {},
    events: [],
    interviews: {},
    interviewRounds: {},
    decisions: {},
    relayedItemIds: [],
    roleStartFailures: {},
  };
}

/** Everything a room shows about its active theme branch. */
export interface BranchView {
  branch: ThemeBranch;
  roleSessions: RoleSessionLink[];
  board: RequirementsBoard;
  proposals: BoardProposal[];
  tasks: WorkflowTask[];
  gates: GateRequest[];
  events: WorkflowEvent[];
  interviews: InterviewQuestion[];
  /** The latest interview, whose questions the progress bar shows. */
  currentInterview: InterviewRound | null;
  decisions: CoordinatorDecision[];
  /** Per-role diagnosis, added when the room loads the branch. */
  health?: RoleHealth[];
}
