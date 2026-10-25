// Diagnoses failing role sessions and runs the PO's recovery actions, so a harness
// crash or a spent quota is visible and fixable instead of looking like progress.

import { AGENT_ROLES, COORDINATOR_ROLE, roleTitle, type AgentRoleId } from "../shared/sdlcTheme.ts";
import {
  ACTION_LABELS,
  DEFAULT_REMEDIATION,
  PROBLEM_TITLES,
  classifyFailure,
  harnessReadiness,
  recommendedActions,
  type FailureDetail,
  type ProblemCategory,
  type RecoveryAction,
  type RoleHealth,
  type RoleHealthState,
  type RoleProblem,
  type ScheduledRetry,
  type TroubleshootState,
} from "../shared/troubleshoot.ts";
import type { RoleSessionLink, RoleStartFailure, ThemeBranch } from "../shared/workflowTypes.ts";
import type { BuildingService } from "./building.ts";
import { roleHarness } from "./bundle.ts";
import type { Notifier } from "./notifier.ts";
import type { OmnigentClient, OmnigentSessionListItem } from "./omnigentClient.ts";
import type { StateStore } from "./stateStore.ts";
import { WorkflowError, type WorkflowService } from "./workflow.ts";

/** A turn with no new items for this long is reported as possibly stuck. */
export const RUNNING_STALL_SECONDS = 15 * 60;
export const LAUNCH_STALL_SECONDS = 3 * 60;
/** How long the PM may sit idle mid-interview before the office says so. */
export const NO_RESPONSE_SECONDS = 2 * 60;
const MAX_SCHEDULED_ATTEMPTS = 48;
const HOSTS_TTL_MS = 20_000;
const CATALOG_TTL_MS = 5 * 60_000;

// The same continuation Omnigent's own retry sends for a turn that died mid-response.
export const CONTINUE_TEXT = "Please continue from where you left off.";

export interface DiagnoseInput {
  now: number;
  role: AgentRoleId;
  link: RoleSessionLink | null;
  /** The session as Omnigent lists it; null when the link's session is gone. */
  session: OmnigentSessionListItem | null;
  /** Omnigent's `last_task_error` for a failed session. */
  failedDetail: FailureDetail | null;
  /** The error the latest turn ended with, from the history scan. */
  lastTurnError: FailureDetail | null;
  startFailure: RoleStartFailure | null;
  /** PM only: the interview is waiting on the PM. */
  awaiting: { since: number; reason: string } | null;
  hostName: string | null;
  harness: string;
  /** The harness's own setup steps from Omnigent, for sign-in and install hints. */
  setupSteps: SetupStep[];
}

export /** Omnigent's own remediation wins, then the harness's setup step, then the office's advice. */
function remediationFor(category: ProblemCategory, failure: FailureDetail | null, steps: SetupStep[]): string | null {
  if (failure?.remediation) return failure.remediation;
  if (category === "auth" || category === "harness_setup") {
    const step = steps.find((entry) => entry.kind === (category === "auth" ? "auth" : "install") && entry.command);
    if (step?.command) return `${step.title ?? "Set up the harness"}: run \`${step.command}\` on the host.`;
  }
  return DEFAULT_REMEDIATION[category] ?? null;
}

function problem(
  input: DiagnoseInput,
  category: ProblemCategory,
  detail: string,
  options: { failure?: FailureDetail | null; since?: number | null; actions?: RecoveryAction[] } = {},
): RoleProblem {
  const failure = options.failure ?? null;
  const since = options.since ?? failure?.at ?? null;
  const subject = input.link?.sessionId ?? input.role;
  let actions = options.actions ?? recommendedActions(category);
  // A temporary worker serves one task; it cannot be restarted or moved on its own.
  if (input.link?.temporary) actions = actions.filter((action) => action !== "restart" && action !== "switch_harness");
  return {
    key: [category, subject, since ?? "", failure?.code ?? ""].join(":"),
    category,
    title: PROBLEM_TITLES[category],
    detail,
    cause: failure?.cause ?? (failure?.title && failure.title !== detail ? failure.title : null),
    remediation: remediationFor(category, failure, input.setupSteps),
    code: failure?.code ?? null,
    since,
    actions,
  };
}

/** One role's health. Failure and loss outrank everything, so neither looks like progress. */
export function diagnoseRole(input: DiagnoseInput): { state: RoleHealthState; problem: RoleProblem | null } {
  const { link, session, now } = input;
  if (!link) {
    if (input.startFailure) {
      const failure: FailureDetail = { code: input.startFailure.code, message: input.startFailure.message, at: input.startFailure.at };
      const kind = classifyFailure(failure);
      const found = problem(input, "start_failed", `Could not start on ${input.startFailure.harness}: ${failure.message}`, { failure });
      return {
        state: "problem",
        problem: {
          ...found,
          cause: kind === "unknown" ? null : PROBLEM_TITLES[kind],
          remediation: kind === "unknown" ? found.remediation : remediationFor(kind, failure, input.setupSteps),
        },
      };
    }
    if (input.role === COORDINATOR_ROLE) {
      return {
        state: "problem",
        problem: problem(input, "missing", "No PM session is running, so nothing coordinates the room.", { actions: ["start", "switch_harness"] }),
      };
    }
    return { state: "not_started", problem: null };
  }
  if (!session) {
    return { state: "problem", problem: problem(input, "missing", `Session ${link.sessionId} was deleted or archived in Omnigent.`) };
  }
  if (session.host_online === false) {
    const host = input.hostName ? `Host ${input.hostName}` : "The session's host";
    return { state: "problem", problem: problem(input, "host_offline", `${host} is not connected to Omnigent.`, { since: session.updated_at }) };
  }
  if (session.status === "failed") {
    const failure = input.failedDetail ?? input.lastTurnError;
    if (!failure) return { state: "problem", problem: problem(input, "unknown", "Omnigent marked the session failed without an error message.", { since: session.updated_at }) };
    // Omnigent's last_task_error has no timestamp; the session's update time marks a recurrence.
    return { state: "problem", problem: problem(input, classifyFailure(failure), failure.message, { failure, since: failure.at ?? session.updated_at }) };
  }
  if (session.pending_elicitations_count > 0) return { state: "waiting_human", problem: null };
  const busy = session.status === "running" || session.status === "launching" || session.status === "waiting";
  if (!busy && input.lastTurnError) {
    const failure = input.lastTurnError;
    return { state: "problem", problem: problem(input, classifyFailure(failure), failure.message, { failure }) };
  }
  const quietFor = now - session.updated_at;
  if (session.status === "launching" && quietFor > LAUNCH_STALL_SECONDS) {
    return {
      state: "problem",
      problem: problem(input, "stalled", `The session has been starting for ${Math.round(quietFor / 60)} minutes.`, {
        since: session.updated_at,
        actions: ["recover", "restart", "switch_harness"],
      }),
    };
  }
  if (session.status === "running" && quietFor > RUNNING_STALL_SECONDS) {
    return {
      state: "problem",
      problem: problem(input, "stalled", `The turn is running but nothing new has appeared for ${Math.round(quietFor / 60)} minutes.`, {
        since: session.updated_at,
      }),
    };
  }
  if (busy) return { state: "working", problem: null };
  if (input.awaiting && now - input.awaiting.since > NO_RESPONSE_SECONDS && session.updated_at >= input.awaiting.since) {
    return { state: "problem", problem: problem(input, "no_response", input.awaiting.reason, { since: input.awaiting.since }) };
  }
  return { state: "idle", problem: null };
}

/** Dismissals and notifications are per session, or per role when it has none. */
export function subjectKey(branchId: string, role: AgentRoleId, sessionId: string | null): string {
  return sessionId ?? `${branchId}:${role}`;
}

function failureFromError(value: unknown): FailureDetail | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const text = (field: string) => (typeof record[field] === "string" && record[field] ? (record[field] as string) : undefined);
  const message = text("message");
  if (!message) return null;
  return { code: text("code") ?? "unknown_error", message, title: text("title"), cause: text("cause"), remediation: text("remediation") };
}

interface HostRecord {
  host_id: string;
  name?: string;
  configured_harnesses?: Record<string, unknown>;
}

interface SetupStep {
  kind?: string;
  title?: string;
  detail?: string;
  command?: string | null;
}

export class TroubleshootService {
  private readonly building: BuildingService;
  private readonly workflow: WorkflowService;
  private readonly client: OmnigentClient;
  private readonly store: StateStore;
  private readonly notifier: Notifier;
  private readonly clock: () => number;
  private hosts: { at: number; hosts: HostRecord[] } | null = null;
  private setupSteps: { at: number; steps: Record<string, SetupStep[]> } | null = null;
  private readonly failedDetails = new Map<string, { updatedAt: number; failure: FailureDetail | null }>();
  private readonly problemCounts = new Map<string, number>();
  private ticking = false;

  constructor(options: {
    building: BuildingService;
    workflow: WorkflowService;
    client: OmnigentClient;
    store: StateStore;
    notifier: Notifier;
    clock: () => number;
  }) {
    this.building = options.building;
    this.workflow = options.workflow;
    this.client = options.client;
    this.store = options.store;
    this.notifier = options.notifier;
    this.clock = options.clock;
  }

  private get state(): TroubleshootState {
    return this.store.state.troubleshooting;
  }

  /** Unresolved, undismissed problems in a project, for the building's flag. */
  problemCount(projectId: string): number {
    return this.problemCounts.get(projectId) ?? 0;
  }

  private async hostList(): Promise<HostRecord[]> {
    if (this.hosts && Date.now() - this.hosts.at < HOSTS_TTL_MS) return this.hosts.hosts;
    const result = (await this.client.listHosts().catch(() => null)) as { hosts?: HostRecord[] } | null;
    this.hosts = { at: Date.now(), hosts: result?.hosts ?? this.hosts?.hosts ?? [] };
    return this.hosts.hosts;
  }

  private async harnessSetupSteps(): Promise<Record<string, SetupStep[]>> {
    if (this.setupSteps && Date.now() - this.setupSteps.at < CATALOG_TTL_MS) return this.setupSteps.steps;
    const result = (await this.client.listHarnesses().catch(() => null)) as { setup_steps?: Record<string, SetupStep[]> } | null;
    this.setupSteps = { at: Date.now(), steps: result?.setup_steps ?? this.setupSteps?.steps ?? {} };
    return this.setupSteps.steps;
  }

  private async failedDetail(session: OmnigentSessionListItem): Promise<FailureDetail | null> {
    if (session.status !== "failed") return null;
    const cached = this.failedDetails.get(session.id);
    if (cached && cached.updatedAt === session.updated_at) return cached.failure;
    const detail = await this.client.getSession(session.id).catch(() => null);
    const failure = failureFromError(detail?.last_task_error);
    this.failedDetails.set(session.id, { updatedAt: session.updated_at, failure });
    return failure;
  }

  /** Health of every role in a branch: its persistent session, then any temporary workers. */
  async health(branchId: string): Promise<RoleHealth[]> {
    const branch = this.workflow.branch(branchId);
    const snapshot = await this.building.getSnapshot();
    if (!snapshot.connection.ok) return [];
    const hosts = await this.hostList();
    const host = branch.runtime.hostId ? hosts.find((entry) => entry.host_id === branch.runtime.hostId) : undefined;
    const steps = await this.harnessSetupSteps();
    const live = this.workflow.view(branchId).roleSessions.filter((link) => link.retiredAt === null);
    const awaiting = this.workflow.interviewAwaitingCoordinator(branchId);
    const now = this.clock();
    const entries: RoleHealth[] = [];
    for (const role of AGENT_ROLES) {
      const harness = roleHarness(branch.runtime, role);
      const links: (RoleSessionLink | null)[] = live.filter((link) => link.role === role).sort((a, b) => Number(a.temporary) - Number(b.temporary));
      if (!links.some((link) => link && !link.temporary)) links.unshift(null);
      for (const link of links) {
        const session = link ? this.building.session(link.sessionId) : null;
        const diagnosis = diagnoseRole({
          now,
          role,
          link,
          session,
          failedDetail: session ? await this.failedDetail(session) : null,
          lastTurnError: link ? (this.building.sessionScan(link.sessionId)?.lastTurnError ?? null) : null,
          startFailure: link ? null : this.workflow.roleStartFailure(branchId, role),
          awaiting: role === COORDINATOR_ROLE && link && !link.temporary ? awaiting : null,
          hostName: host?.name ?? null,
          harness,
          setupSteps: steps[harness] ?? [],
        });
        const subject = subjectKey(branchId, role, link?.sessionId ?? null);
        const readiness = branch.runtime.hostId ? harnessReadiness(harness, host?.configured_harnesses) : "unknown";
        const found = diagnosis.problem;
        entries.push({
          role,
          sessionId: link?.sessionId ?? null,
          temporary: link?.temporary ?? false,
          harness,
          model: branch.runtime.roleModels?.[role] ?? branch.runtime.model ?? null,
          harnessReadiness: readiness,
          state: diagnosis.state,
          problem: found,
          dismissed: Boolean(found && this.state.dismissed[subject] === found.key),
          scheduledRetry: link ? (this.state.retries[link.sessionId] ?? null) : null,
        });
      }
    }
    this.afterDiagnosis(branch, entries);
    return entries;
  }

  private afterDiagnosis(branch: ThemeBranch, entries: RoleHealth[]): void {
    const open = entries.filter((entry) => entry.problem && !entry.dismissed);
    this.problemCounts.set(branch.projectId, open.length);
    let changed = false;
    for (const entry of open) {
      const subject = subjectKey(branch.id, entry.role, entry.sessionId);
      const found = entry.problem as RoleProblem;
      if (this.state.notified[subject] === found.key) continue;
      this.state.notified[subject] = found.key;
      changed = true;
      void this.roomName(branch.projectId).then((room) =>
        this.notifier.notify(`${room}: ${roleTitle(entry.role)} needs help`, `${found.title}. ${found.detail}`.slice(0, 240)),
      );
    }
    if (changed) this.store.save();
  }

  private async roomName(projectId: string): Promise<string> {
    const snapshot = await this.building.getSnapshot().catch(() => null);
    return snapshot?.rooms.find((room) => room.projectId === projectId)?.name ?? "A room";
  }

  private async entry(branchId: string, sessionId: string): Promise<RoleHealth> {
    const found = (await this.health(branchId)).find((entry) => entry.sessionId === sessionId);
    if (!found) throw new WorkflowError(404, "session_not_found", "That session is not a live role session in this room.");
    return found;
  }

  /** Run one recovery action on a role session; returns what happened, for the PO. */
  async act(branchId: string, sessionId: string, actionInput: unknown): Promise<{ result: string }> {
    const action = actionInput as RecoveryAction;
    if (!["continue", "recover", "nudge", "stop"].includes(action)) {
      throw new WorkflowError(400, "invalid_action", "Use continue, recover, nudge, or stop here.");
    }
    const target = await this.entry(branchId, sessionId);
    const result = await this.perform(branchId, target, action as "continue" | "recover" | "nudge" | "stop");
    delete this.state.retries[sessionId];
    this.store.save();
    this.workflow.logRecovery(branchId, `PO chose "${ACTION_LABELS[action]}" for the ${roleTitle(target.role)}: ${result}`);
    this.building.invalidate();
    return { result };
  }

  private async perform(branchId: string, target: RoleHealth, action: "continue" | "recover" | "nudge" | "stop"): Promise<string> {
    const sessionId = target.sessionId as string;
    if (action === "stop") {
      await this.client.interrupt(sessionId);
      return "Stop sent.";
    }
    if (action === "recover") {
      const outcome = (await this.client.retrySession(sessionId)) as { recovered?: boolean; recovery?: string };
      if (!outcome.recovered) return "Omnigent reports the runtime is already connected; try Continue the turn or Restart with handoff.";
      return outcome.recovery === "runner_relaunched" ? "The runtime was relaunched." : "The runtime is ready again.";
    }
    if (target.state === "working") throw new WorkflowError(409, "turn_running", "The agent is working; stop the turn first or wait for it to end.");
    const text = action === "continue" ? CONTINUE_TEXT : this.nudgeText(branchId, target.role);
    const sent = await this.building.sendMessage(sessionId, text);
    if (sent.denied) throw new WorkflowError(403, "message_denied", "A policy blocked the message.");
    return action === "continue" ? "Asked the agent to continue its turn." : "Reminded the agent what it was asked to do.";
  }

  private nudgeText(branchId: string, role: AgentRoleId): string {
    if (role === COORDINATOR_ROLE && this.workflow.interviewAwaitingCoordinator(branchId)) {
      return "Please continue the interview: plan your questions with office_interview_plan if you have none, then ask the next one with office_interview_ask.";
    }
    return "You seem to have stopped. Check your open tasks with office_board_get and continue, or report what blocks you with office_post_update.";
  }

  /** Retry once after a delay, or keep retrying while the problem lasts. */
  async scheduleRetry(branchId: string, sessionId: string, minutesInput: unknown, repeatInput: unknown): Promise<{ retry: ScheduledRetry | null }> {
    const target = await this.entry(branchId, sessionId);
    if (minutesInput === null) {
      delete this.state.retries[sessionId];
      this.store.save();
      this.workflow.logRecovery(branchId, `PO cancelled the scheduled retry for the ${roleTitle(target.role)}.`);
      return { retry: null };
    }
    const minutes = Number(minutesInput);
    if (!Number.isFinite(minutes) || minutes < 1 || minutes > 24 * 60) throw new WorkflowError(400, "invalid_delay", "Choose a delay between 1 minute and 24 hours.");
    const category = target.problem?.category;
    const retry: ScheduledRetry = {
      sessionId,
      branchId,
      // A lost runtime needs reconnecting; a failed model call needs the turn resent.
      action: category === "runner" || category === "harness_crash" || category === "host_offline" ? "recover" : "continue",
      nextAt: this.clock() + Math.round(minutes * 60),
      repeatMinutes: repeatInput === true ? minutes : null,
      attempts: 0,
      lastResult: null,
    };
    this.state.retries[sessionId] = retry;
    this.store.save();
    this.workflow.logRecovery(
      branchId,
      `PO scheduled a retry for the ${roleTitle(target.role)} in ${minutes} minute${minutes === 1 ? "" : "s"}${retry.repeatMinutes ? ", repeating until it recovers" : ""}.`,
    );
    return { retry };
  }

  /** Ignore one occurrence of a problem; a new failure flags the room again. */
  async dismiss(branchId: string, subjectInput: unknown, keyInput: unknown): Promise<void> {
    const entries = await this.health(branchId);
    const target = entries.find((entry) => subjectKey(branchId, entry.role, entry.sessionId) === subjectInput);
    if (!target?.problem || target.problem.key !== keyInput) throw new WorkflowError(409, "stale_request", "That problem has changed; review it again.");
    this.state.dismissed[subjectKey(branchId, target.role, target.sessionId)] = target.problem.key;
    this.store.save();
    this.workflow.logRecovery(branchId, `PO ignored "${target.problem.title}" for the ${roleTitle(target.role)}.`);
    this.problemCounts.set(this.workflow.branch(branchId).projectId, entries.filter((entry) => entry.problem && !entry.dismissed && entry !== target).length);
    this.building.invalidate();
  }

  /** Background pass: notify about new problems and run due retries, with the browser closed too. */
  async tick(): Promise<void> {
    if (this.ticking) return;
    this.ticking = true;
    try {
      for (const branch of this.workflow.activeBranches()) {
        const entries = await this.health(branch.id).catch(() => [] as RoleHealth[]);
        for (const entry of entries) {
          const retry = entry.sessionId ? this.state.retries[entry.sessionId] : undefined;
          if (retry && retry.nextAt <= this.clock()) await this.runRetry(branch.id, entry, retry);
        }
      }
    } finally {
      this.ticking = false;
    }
  }

  private async runRetry(branchId: string, entry: RoleHealth, retry: ScheduledRetry): Promise<void> {
    const sessionId = retry.sessionId;
    if (!entry.problem) {
      delete this.state.retries[sessionId];
      this.store.save();
      this.workflow.logRecovery(branchId, `The ${roleTitle(entry.role)} recovered; the scheduled retry is no longer needed.`);
      return;
    }
    if (entry.state === "working") {
      retry.nextAt = this.clock() + 60;
      this.store.save();
      return;
    }
    let result: string;
    try {
      result = await this.perform(branchId, entry, retry.action);
    } catch (error) {
      result = `failed: ${(error as Error).message}`;
    }
    retry.attempts += 1;
    retry.lastResult = result;
    if (retry.repeatMinutes && retry.attempts < MAX_SCHEDULED_ATTEMPTS) retry.nextAt = this.clock() + retry.repeatMinutes * 60;
    else delete this.state.retries[sessionId];
    this.store.save();
    this.workflow.logRecovery(branchId, `Scheduled retry ${retry.attempts} for the ${roleTitle(entry.role)}: ${result}`);
    this.building.invalidate();
  }
}
