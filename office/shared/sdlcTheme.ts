// The SDLC theme: roles, the user-supplied RACI matrix, default human gates,
// and the runtime gate patterns. Shared by the service and the browser.

export type RoleId = "pm" | "po" | "architect" | "developer" | "reviewer" | "qa" | "devops";
export type AgentRoleId = Exclude<RoleId, "po">;
export type Raci = "R" | "A" | "RA" | "C" | "I";
export type GateType = "requirements" | "architecture" | "merge" | "deployment" | "outage_triage";

export interface RoleDefinition {
  id: RoleId;
  title: string;
  /** Persistent role conversations exist for agent roles; the PO is the human. */
  human: boolean;
  /** Sprite palette index for the role's appearance. */
  appearance: number;
}

export const ROLES: readonly RoleDefinition[] = [
  { id: "pm", title: "PM", human: false, appearance: 0 },
  { id: "po", title: "PO", human: true, appearance: -1 },
  { id: "architect", title: "Architect", human: false, appearance: 1 },
  { id: "developer", title: "Developer", human: false, appearance: 2 },
  { id: "reviewer", title: "Reviewing Developer", human: false, appearance: 3 },
  { id: "qa", title: "QA", human: false, appearance: 4 },
  { id: "devops", title: "DevOps", human: false, appearance: 5 },
];

export const AGENT_ROLES: readonly AgentRoleId[] = ["pm", "architect", "developer", "reviewer", "qa", "devops"];
/** Default agent harness per role: Developer/QA on Claude, Architect/PM on Codex, DevOps on Gemini (agy), Reviewing Developer on Copilot. */
export const DEFAULT_ROLE_HARNESSES: Readonly<Record<AgentRoleId, string>> = {
  developer: "claude-sdk",
  architect: "codex",
  pm: "codex",
  devops: "agy",
  qa: "claude-sdk",
  reviewer: "copilot",
};

export const COORDINATOR_ROLE: AgentRoleId = "pm";

export function roleTitle(role: RoleId): string {
  return ROLES.find((definition) => definition.id === role)?.title ?? role;
}

export function isAgentRole(value: unknown): value is AgentRoleId {
  return typeof value === "string" && (AGENT_ROLES as readonly string[]).includes(value);
}

export interface MatrixRow {
  id: string;
  title: string;
  assignments: Record<RoleId, Raci>;
  /** Default human gate attached to tasks of this kind. */
  gate: GateType | null;
}

function row(
  id: string,
  title: string,
  [pm, po, architect, developer, reviewer, qa, devops]: Raci[],
  gate: GateType | null = null,
): MatrixRow {
  return { id, title, assignments: { pm, po, architect, developer, reviewer, qa, devops }, gate };
}

// Columns: PM, PO, Architect, Developer, Reviewing Developer, QA, DevOps.
export const SDLC_MATRIX: readonly MatrixRow[] = [
  row("requirements", "Gather and document requirements", ["C", "RA", "C", "I", "I", "I", "I"], "requirements"),
  row("scope", "Define scope and timeline", ["RA", "C", "C", "I", "I", "I", "I"]),
  row("architecture", "Define architecture and stack", ["I", "I", "RA", "C", "I", "I", "C"], "architecture"),
  row("api_design", "Design APIs and database", ["I", "I", "A", "R", "I", "I", "I"]),
  row("implementation", "Write code and unit tests", ["I", "I", "C", "RA", "I", "I", "I"]),
  // A separate Reviewing Developer reviews; the authoring Developer is consulted.
  row("code_review", "Peer code review", ["I", "I", "R", "C", "RA", "I", "I"], "merge"),
  row("test_plan", "Create test plans and cases", ["I", "C", "I", "I", "I", "RA", "I"]),
  row("test_execution", "Execute tests and report bugs", ["I", "I", "I", "C", "I", "RA", "I"]),
  row("bug_fix", "Fix bugs", ["I", "I", "I", "RA", "I", "C", "I"]),
  row("ci_cd", "Set up CI/CD", ["I", "I", "C", "I", "I", "I", "RA"]),
  row("deployment", "Production deployment", ["C", "I", "C", "I", "I", "I", "RA"], "deployment"),
  row("monitoring", "Monitor system health", ["I", "I", "C", "I", "I", "I", "RA"]),
  row("outage", "Triage and resolve outages", ["C", "I", "C", "RA", "I", "C", "R"], "outage_triage"),
];

export function matrixRow(taskType: string): MatrixRow | undefined {
  return SDLC_MATRIX.find((entry) => entry.id === taskType);
}

export function responsibleRoles(entry: MatrixRow): RoleId[] {
  return ROLES.map((role) => role.id).filter((role) => entry.assignments[role].includes("R"));
}

export function accountableRole(entry: MatrixRow): RoleId {
  const roles = ROLES.map((role) => role.id).filter((role) => entry.assignments[role].includes("A"));
  if (roles.length !== 1) throw new Error(`Matrix row ${entry.id} must have exactly one accountable role`);
  return roles[0];
}

export function rolesWith(entry: MatrixRow, letter: "C" | "I"): RoleId[] {
  return ROLES.map((role) => role.id).filter((role) => entry.assignments[role] === letter);
}

export const GATE_TITLES: Record<GateType, string> = {
  requirements: "Requirements",
  architecture: "Architecture",
  merge: "Merge",
  deployment: "Deployment",
  outage_triage: "Outage triage",
};

export const DEFAULT_GATES: Record<GateType, boolean> = {
  requirements: true,
  architecture: true,
  merge: true,
  deployment: true,
  outage_triage: false,
};

/** When a gate on the left goes stale, later gates on the right go stale with it. */
export const GATE_INVALIDATES: Partial<Record<GateType, GateType[]>> = {
  requirements: ["architecture"],
  architecture: ["merge"],
  merge: ["deployment"],
};

/**
 * Shell commands each runtime-enforced gate mediates (RE2 syntax, evaluated by
 * an Omnigent CEL session policy). A command matching a pattern is denied
 * unless it is exactly an approved operation.
 */
export const RUNTIME_GATE_PATTERNS: Partial<Record<GateType, string>> = {
  merge: String.raw`\bgh\s+pr\s+merge\b|\bgit\s+push\b[^;&|]*\b(main|master)\b|\bgit\s+merge\b`,
  deployment: String.raw`\b(kubectl|helm|terraform|databricks|pulumi|flyctl|vercel)\b.*\b(apply|deploy|up|destroy|release)\b|\bnpm\s+run\s+deploy\b|\bmake\s+deploy\b`,
};

/** MCP tool names that perform the same gated operations outside the shell. */
export const RUNTIME_GATE_TOOLS: Partial<Record<GateType, string[]>> = {
  merge: ["merge_pull_request", "github_merge_pull_request"],
};

/** Every tool name Omnigent harnesses use for shell execution. */
export const SHELL_TOOL_NAMES = ["sys_os_shell", "Bash", "bash", "Shell", "terminal", "developer__shell", "shell"];

export const ROLE_BRIEFS: Record<AgentRoleId, string> = {
  pm: [
    "You are the PM and the coordinator of this SDLC project room. The human is the Product Owner (PO).",
    "Plan work as tasks with office_task_create (each task names its matrix row), dispatch them with",
    "office_task_dispatch, and keep the board current. You may draft requirements with",
    "office_board_propose, but only the PO accepts them. For an interview, first list your questions with",
    "office_interview_plan, then ask exactly one at a time with office_interview_ask and wait for the answer",
    "before asking the next. Add follow-ups with office_interview_plan and drop unneeded ones with office_interview_skip.",
    "Route any change to shared scope, interfaces, sequencing, or commitments through a recorded decision",
    "(office_record_decision). Never claim a gate is approved until the office reports it.",
  ].join(" "),
  architect: [
    "You are the Architect. You own the architecture and stack and are accountable for API and database",
    "design. Produce your deliverable in the repository, then request the architecture gate with",
    "office_gate_request, citing the file path and its content digest.",
  ].join(" "),
  developer: [
    "You are the Developer. You write code and unit tests, design APIs and databases, and fix bugs.",
    "You never review your own code. Report progress and evidence with office_task_update.",
  ].join(" "),
  reviewer: [
    "You are the Reviewing Developer, accountable for peer code review. You did not write the code you",
    "review. Record objections in office_task_update; when the change is ready, request the merge gate",
    "with office_gate_request naming the exact merge command and the commit you reviewed.",
  ].join(" "),
  qa: [
    "You are QA. You write test plans and cases, execute tests, and report bugs with evidence through",
    "office_task_update.",
  ].join(" "),
  devops: [
    "You are DevOps. You own CI/CD, production deployment, and monitoring. Before any production",
    "deployment request the deployment gate with office_gate_request, naming the exact command, the",
    "target environment, and the commit being deployed. The runtime blocks that command until approved.",
  ].join(" "),
};

export const SDLC_WORKFLOW_RULES = [
  "Workflow rules for every role:",
  "- Every task has a deliverable, acceptance criteria, responsible and accountable roles, dependencies,",
  "  an optional human gate, and the evidence required for completion. Read them with office_board_get.",
  "- Work only on tasks dispatched to you. Report progress with office_task_update; submit evidence when done.",
  "- Only the accountable role completes a task (office_task_complete).",
  "- Direct PO instructions are followed unless they affect other roles; then ask the PM to record a decision.",
  "- A stop instruction from the PO takes effect immediately.",
  "- A human approval satisfies a workflow gate only. It never grants credentials or bypasses runtime policy.",
  "- Waiting on a gate blocks only dependent tasks; keep working on independent ones.",
].join("\n");
