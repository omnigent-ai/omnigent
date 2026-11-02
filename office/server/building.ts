// Assembles building snapshots from Omnigent projects and sessions, and
// mediates the session actions the office exposes.

import { randomUUID } from "node:crypto";
import { type AutoApproveRule, isPlainApproval, nativeSessionGrant } from "../shared/sessionApproval.ts";
import { MAX_ROOM_MESSAGES, type RoomMessage } from "../shared/roomMessages.ts";
import type { BuildingSnapshot, Priority, RoomSettingsPatch, RoomSummary } from "../shared/types.ts";
import { mergeScan, scanSession, type SessionScan } from "./activity.ts";
import { OmnigentError, type OmnigentClient, type OmnigentSessionListItem } from "./omnigentClient.ts";
import { silentNotifier, type Notifier } from "./notifier.ts";
import type { ApprovalDecision, StateStore } from "./stateStore.ts";
import type { WorkflowService } from "./workflow.ts";

const PRIORITIES: readonly Priority[] = ["normal", "high", "critical"];

interface PendingRequest {
  elicitation_id?: string;
  params?: Record<string, unknown>;
}

export type WorkerListItem = OmnigentSessionListItem & { office_auto_approve: AutoApproveRule | null };
const MIN_REFRESH_INTERVAL_MS = 4_000;

export class OfficeRequestError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export type Clock = () => number;

const systemClock: Clock = () => Math.floor(Date.now() / 1000);

export class BuildingService {
  private readonly client: OmnigentClient;
  private readonly store: StateStore;
  private readonly clock: Clock;
  private sessions: OmnigentSessionListItem[] = [];
  private snapshot: BuildingSnapshot | null = null;
  private lastRefreshMs = 0;
  private inFlight: Promise<BuildingSnapshot> | null = null;
  private workflow: WorkflowService | null = null;
  private problemCount: (projectId: string) => number = () => 0;
  private readonly notifier: Notifier;
  /** Pending Omnigent requests per session at the last refresh, to notify only on new ones. */
  private pendingSeen: Map<string, number> | null = null;

  constructor(
    client: OmnigentClient,
    store: StateStore,
    clock: Clock = systemClock,
    options: { notifier?: Notifier } = {},
  ) {
    this.client = client;
    this.store = store;
    this.clock = clock;
    this.notifier = options.notifier ?? silentNotifier;
  }

  attachWorkflow(workflow: WorkflowService): void {
    this.workflow = workflow;
  }

  /** Source of the per-room count of agent problems the PO has not dismissed. */
  attachProblemCounts(count: (projectId: string) => number): void {
    this.problemCount = count;
  }

  /** A session from the latest refresh, or null when Omnigent no longer lists it. */
  session(sessionId: string): OmnigentSessionListItem | null {
    return this.sessions.find((session) => session.id === sessionId) ?? null;
  }

  sessionScan(sessionId: string): SessionScan | undefined {
    return this.store.state.sessions[sessionId];
  }

  /** Return a recent snapshot, refreshing from Omnigent when it is stale. */
  async getSnapshot(force = false): Promise<BuildingSnapshot> {
    const fresh = Date.now() - this.lastRefreshMs < MIN_REFRESH_INTERVAL_MS;
    if (this.snapshot && fresh && !force) return this.snapshot;
    this.inFlight ??= this.refresh().finally(() => {
      this.inFlight = null;
    });
    return this.inFlight;
  }

  private async refresh(): Promise<BuildingSnapshot> {
    const now = this.clock();
    try {
      const [projects, sessions] = await Promise.all([this.client.listProjects(), this.client.listSessions()]);
      this.sessions = sessions.filter((session) => !session.archived);
      await this.autoApprovePending();
      this.notifyNewRequests(projects);
      await this.scanChangedSessions();
      const rooms = projects.map((project) => this.summarize(project, now));
      this.store.save();
      this.snapshot = { serverNow: now, rooms, connection: { omnigentUrl: this.client.baseUrl, ok: true } };
    } catch (error) {
      // Keep showing the last known building, flagged as disconnected.
      this.snapshot = {
        serverNow: now,
        rooms: this.snapshot?.rooms ?? [],
        connection: { omnigentUrl: this.client.baseUrl, ok: false, error: (error as Error).message },
      };
    }
    this.lastRefreshMs = Date.now();
    return this.snapshot;
  }

  private notifyNewRequests(projects: { id: string; name: string }[]): void {
    const seen = new Map(this.sessions.map((session) => [session.id, session.pending_elicitations_count]));
    if (this.pendingSeen) {
      for (const session of this.sessions) {
        if (!session.project_id || session.pending_elicitations_count <= (this.pendingSeen.get(session.id) ?? 0)) continue;
        const room = projects.find((project) => project.id === session.project_id)?.name ?? "A room";
        this.notifier.notify(`${room}: approval needed`, `${session.title ?? "A worker"} is waiting for your decision.`);
      }
    }
    this.pendingSeen = seen;
  }

  private async scanChangedSessions(): Promise<void> {
    const changed = this.sessions.filter(
      (session) =>
        session.project_id !== null &&
        this.store.state.sessions[session.id]?.scannedUpdatedAt !== session.updated_at,
    );
    // Scan a few at a time to avoid flooding the server on first sync.
    const concurrency = 4;
    for (let index = 0; index < changed.length; index += concurrency) {
      await Promise.all(
        changed.slice(index, index + concurrency).map(async (session) => {
          try {
            const scan = await scanSession(
              session.id,
              session.updated_at,
              (sessionId, after) => this.client.listItems(sessionId, { after }),
              undefined,
              (itemId) => this.workflow?.isRelayedItem(itemId) ?? false,
            );
            this.store.state.sessions[session.id] = mergeScan(this.store.state.sessions[session.id], scan);
          } catch {
            // Leave the previous scan in place; an unscanned session marks history incomplete.
          }
        }),
      );
    }
  }

  private summarize(project: { id: string; name: string; created_at?: number; config?: { icon?: string; host_id?: string; workspace?: string } }, now: number): RoomSummary {
    const roomState = this.store.ensureRoom(project.id, now);
    const sessions = this.sessions.filter((session) => session.project_id === project.id);
    let latestInteractionAt: number | null = null;
    let historyComplete = true;
    for (const session of sessions) {
      const scan = this.store.state.sessions[session.id];
      if (!scan || !scan.complete) historyComplete = false;
      if (scan?.latestHumanAt != null) latestInteractionAt = Math.max(latestInteractionAt ?? 0, scan.latestHumanAt);
    }
    for (const decision of Object.values(this.store.state.decisions)) {
      if (decision.projectId === project.id) {
        latestInteractionAt = Math.max(latestInteractionAt ?? 0, decision.decidedAt);
      }
    }
    const workflowDecisionAt = this.workflow?.latestDecisionAt(project.id) ?? null;
    if (workflowDecisionAt !== null) latestInteractionAt = Math.max(latestInteractionAt ?? 0, workflowDecisionAt);
    const branch = this.workflow?.activeBranch(project.id) ?? null;
    return {
      projectId: project.id,
      name: project.name,
      theme: branch?.theme ?? null,
      icon: project.config?.icon,
      hostId: project.config?.host_id,
      workspace: project.config?.workspace,
      createdAt: project.created_at ?? roomState.firstSyncAt,
      priority: roomState.priority,
      keepUpstairs: roomState.keepUpstairs,
      latestInteractionAt,
      firstSyncAt: roomState.firstSyncAt,
      historyComplete,
      pendingRequestCount:
        sessions.reduce((total, session) => total + session.pending_elicitations_count, 0) +
        (this.workflow?.pendingHumanRequests(project.id) ?? 0),
      agentProblemCount: this.problemCount(project.id),
      workerCount: sessions.length,
      activeWorkerCount: sessions.filter((session) => session.status === "running" || session.status === "launching").length,
    };
  }

  async updateRoom(projectId: string, patch: RoomSettingsPatch): Promise<BuildingSnapshot> {
    if (patch.priority !== undefined && !PRIORITIES.includes(patch.priority)) {
      throw new OfficeRequestError(400, "invalid_priority", `priority must be one of ${PRIORITIES.join(", ")}`);
    }
    if (patch.keepUpstairs !== undefined && typeof patch.keepUpstairs !== "boolean") {
      throw new OfficeRequestError(400, "invalid_keep_upstairs", "keepUpstairs must be a boolean");
    }
    const snapshot = await this.getSnapshot();
    if (!snapshot.rooms.some((room) => room.projectId === projectId)) {
      throw new OfficeRequestError(404, "room_not_found", "No such project");
    }
    const room = this.store.ensureRoom(projectId, this.clock());
    if (patch.priority !== undefined) room.priority = patch.priority;
    if (patch.keepUpstairs !== undefined) room.keepUpstairs = patch.keepUpstairs;
    this.store.save();
    return this.getSnapshot(true);
  }

  /** Create a project whose config carries its default host and folder, as Omnigent's own UI stores them. */
  async createRoom(input: { name?: unknown; icon?: unknown; hostId?: unknown; workspace?: unknown }): Promise<{ projectId: string }> {
    const name = typeof input.name === "string" ? input.name.trim() : "";
    if (!name || name.length > 100) throw new OfficeRequestError(400, "invalid_name", "Give the room a name (up to 100 characters).");
    const config: Record<string, string> = {};
    const icon = typeof input.icon === "string" ? [...input.icon.trim()].slice(0, 2).join("") : "";
    if (icon) config.icon = icon;
    const hostId = typeof input.hostId === "string" ? input.hostId.trim() : "";
    const workspace = typeof input.workspace === "string" ? input.workspace.trim() : "";
    if (workspace && !hostId) throw new OfficeRequestError(400, "invalid_workspace", "Choose the host the folder is on.");
    if (hostId && !workspace) throw new OfficeRequestError(400, "invalid_workspace", "Choose a folder on the host.");
    if (hostId) config.host_id = hostId;
    if (workspace) config.workspace = workspace;
    const project = await this.client.createProject(name, config);
    this.invalidate();
    return { projectId: project.id };
  }

  /** Mark the snapshot stale so the next read refreshes from Omnigent. */
  invalidate(): void {
    this.lastRefreshMs = 0;
  }

  async listWorkers(projectId: string): Promise<WorkerListItem[]> {
    await this.getSnapshot();
    return this.sessions
      .filter((session) => session.project_id === projectId)
      .map((session) => ({ ...session, office_auto_approve: this.store.state.autoApprove[session.id] ?? null }));
  }

  hasAutoApproveRules(): boolean {
    return Object.keys(this.store.state.autoApprove).length > 0;
  }

  /** Stop approving a session's requests automatically. Turning it on happens by approving a request. */
  disableAutoApprove(sessionId: string): { enabled: false } {
    const rule = this.store.state.autoApprove[sessionId];
    if (rule) {
      delete this.store.state.autoApprove[sessionId];
      this.store.save();
      this.logForSession(sessionId, "auto_approve_off", "PO stopped approving this session's requests automatically.");
    }
    return { enabled: false };
  }

  private logForSession(sessionId: string, kind: string, summary: string): void {
    const session = this.session(sessionId);
    const branchId = session?.labels?.["office.branch"];
    if (!branchId) return;
    const role = session?.title ?? "A worker";
    this.workflow?.logPoEvent(branchId, kind, `${role}: ${summary}`);
  }

  /** Answer plain approval requests of sessions the PO approved for the rest of the session. */
  private async autoApprovePending(): Promise<void> {
    const rules = this.store.state.autoApprove;
    const listed = new Set(this.sessions.map((session) => session.id));
    for (const sessionId of Object.keys(rules)) if (!listed.has(sessionId)) delete rules[sessionId];
    for (const session of this.sessions) {
      const rule = rules[session.id];
      if (!rule || session.pending_elicitations_count <= 0) continue;
      const detail = await this.client.getSession(session.id).catch(() => null);
      const pending = (detail?.pending_elicitations as PendingRequest[] | undefined) ?? [];
      let resolved = 0;
      for (const request of pending) {
        // Questions and forms still need the PO's own answer.
        if (!request.elicitation_id || !isPlainApproval(request.params)) continue;
        const grant = nativeSessionGrant(request.params);
        try {
          await this.client.resolveElicitation(session.id, request.elicitation_id, "accept", grant?.content, grant?.meta);
        } catch {
          continue;
        }
        resolved++;
        const now = this.clock();
        this.store.state.decisions[`${session.id}:${request.elicitation_id}`] = {
          projectId: session.project_id,
          sessionId: session.id,
          elicitationId: request.elicitation_id,
          action: "accept",
          decidedAt: now,
          automatic: true,
        };
        const message = typeof request.params?.message === "string" ? request.params.message : null;
        Object.assign(rule, { approvedCount: rule.approvedCount + 1, lastApprovedAt: now, lastMessage: message });
        this.logForSession(session.id, "auto_approved", `approved automatically for this session: ${(message ?? "approval request").slice(0, 200)}`);
      }
      session.pending_elicitations_count = Math.max(0, session.pending_elicitations_count - resolved);
    }
  }

  private projectOf(sessionId: string): string | null {
    return this.sessions.find((session) => session.id === sessionId)?.project_id ?? null;
  }

  async sendMessage(sessionId: string, text: string): Promise<{ queued: boolean; denied?: boolean }> {
    if (!text.trim()) throw new OfficeRequestError(400, "empty_message", "Message text is required");
    const result = await this.client.sendMessage(sessionId, text);
    this.lastRefreshMs = 0;
    return result;
  }

  roomMessages(projectId: string): RoomMessage[] {
    return this.store.state.roomMessages[projectId] ?? [];
  }

  /**
   * Send one message from the room to each recipient session and keep it for the room's
   * channel. Recipients must be sessions of this room; the message is kept when any is reached.
   */
  async sendRoomMessage(
    projectId: string,
    textInput: unknown,
    recipientsInput: unknown,
  ): Promise<{ message: RoomMessage | null; failures: { name: string; error: string }[] }> {
    const text = typeof textInput === "string" ? textInput.trim() : "";
    if (!text) throw new OfficeRequestError(400, "empty_message", "Message text is required");
    if (text.length > 20_000) throw new OfficeRequestError(400, "message_too_long", "Messages are limited to 20,000 characters");
    const roomSessions = new Set((await this.listWorkers(projectId)).map((session) => session.id));
    const recipients = (Array.isArray(recipientsInput) ? recipientsInput : [])
      .map((entry) => entry as { sessionId?: unknown; name?: unknown })
      .filter((entry): entry is { sessionId: string; name: string } => typeof entry.sessionId === "string" && typeof entry.name === "string")
      .map((entry) => ({ sessionId: entry.sessionId, name: entry.name.slice(0, 120) }))
      .filter((entry, index, all) => all.findIndex((other) => other.sessionId === entry.sessionId) === index);
    if (recipients.length === 0) throw new OfficeRequestError(400, "no_recipient", "Choose who to message.");
    const outside = recipients.find((entry) => !roomSessions.has(entry.sessionId));
    if (outside) throw new OfficeRequestError(400, "not_in_room", `${outside.name} is not a session of this room.`);

    const failures: { name: string; error: string }[] = [];
    const reached: RoomMessage["recipients"] = [];
    for (const recipient of recipients) {
      try {
        const result = await this.client.sendMessage(recipient.sessionId, text);
        if (result.denied) failures.push({ name: recipient.name, error: "blocked by a policy" });
        else reached.push(recipient);
      } catch (error) {
        failures.push({ name: recipient.name, error: (error as Error).message });
      }
    }
    this.lastRefreshMs = 0;
    if (reached.length === 0) return { message: null, failures };
    const message: RoomMessage = { id: `msg_${randomUUID()}`, at: this.clock(), text, recipients: reached };
    const kept = [...this.roomMessages(projectId), message];
    this.store.state.roomMessages[projectId] = kept.slice(-MAX_ROOM_MESSAGES);
    this.store.save();
    return { message, failures };
  }

  /**
   * Resolve an approval request. The decision is bound to the elicitation id,
   * rejected when that request is no longer pending, and idempotent on retry.
   */
  async decide(
    sessionId: string,
    elicitationId: string,
    action: "accept" | "decline",
    feedback?: string,
    options: { forSession?: boolean } = {},
  ): Promise<{ decision: ApprovalDecision; replayed: boolean; autoApprove: AutoApproveRule | null }> {
    if (action !== "accept" && action !== "decline") {
      throw new OfficeRequestError(400, "invalid_action", "action must be accept or decline");
    }
    const decisionId = `${sessionId}:${elicitationId}`;
    const existing = this.store.state.decisions[decisionId];
    if (existing) {
      if (existing.action !== action) {
        throw new OfficeRequestError(409, "already_decided", `This request was already answered (${existing.action}).`);
      }
      return { decision: existing, replayed: true, autoApprove: this.store.state.autoApprove[sessionId] ?? null };
    }
    const session = await this.client.getSession(sessionId);
    const pending = (session.pending_elicitations as PendingRequest[] | undefined) ?? [];
    const request = pending.find((entry) => entry.elicitation_id === elicitationId);
    if (!request) {
      throw new OfficeRequestError(409, "stale_request", "This request is no longer pending; it was answered, cancelled, or superseded.");
    }
    const forSession = options.forSession === true && action === "accept";
    if (forSession && !isPlainApproval(request.params)) {
      throw new OfficeRequestError(400, "not_plain_approval", "This request needs your own answer each time, so it cannot be approved for the whole session.");
    }
    const grant = forSession ? nativeSessionGrant(request.params) : null;
    await this.client.resolveElicitation(sessionId, elicitationId, action, grant?.content, grant?.meta);
    const decision: ApprovalDecision = {
      projectId: (session.project_id as string | null | undefined) ?? this.projectOf(sessionId),
      sessionId,
      elicitationId,
      action,
      decidedAt: this.clock(),
    };
    this.store.state.decisions[decisionId] = decision;
    if (forSession && !this.store.state.autoApprove[sessionId]) {
      this.store.state.autoApprove[sessionId] = {
        sessionId,
        projectId: decision.projectId,
        since: decision.decidedAt,
        approvedCount: 0,
        lastApprovedAt: null,
        lastMessage: null,
      };
      this.logForSession(sessionId, "auto_approve_on", "PO approved its requests for the rest of this session.");
    }
    this.store.save();
    if (feedback?.trim()) {
      try {
        await this.client.sendMessage(sessionId, feedback.trim());
      } catch (error) {
        if (!(error instanceof OmnigentError)) throw error;
        throw new OfficeRequestError(502, "feedback_not_sent", `Decision recorded, but feedback was not delivered: ${error.message}`);
      }
    }
    this.lastRefreshMs = 0;
    return { decision, replayed: false, autoApprove: this.store.state.autoApprove[sessionId] ?? null };
  }
}
