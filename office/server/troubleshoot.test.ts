import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { RoleSessionLink } from "../shared/workflowTypes.ts";
import type { SessionScan } from "./activity.ts";
import type { BuildingService } from "./building.ts";
import { FakeOmnigent } from "./fakeOmnigent.ts";
import type { OmnigentClient, OmnigentSessionListItem } from "./omnigentClient.ts";
import { StateStore } from "./stateStore.ts";
import { CONTINUE_TEXT, type DiagnoseInput, NO_RESPONSE_SECONDS, RUNNING_STALL_SECONDS, TroubleshootService, diagnoseRole } from "./troubleshoot.ts";
import { WorkflowService } from "./workflow.ts";

const NOW = 1_800_000_000;

function session(id: string, overrides: Partial<OmnigentSessionListItem> = {}): OmnigentSessionListItem {
  return {
    id,
    title: id,
    agent_name: null,
    status: "idle",
    project_id: "proj",
    parent_session_id: null,
    created_at: NOW - 600,
    updated_at: NOW - 60,
    host_online: true,
    runner_online: true,
    pending_elicitations_count: 0,
    archived: false,
    ...overrides,
  };
}

const LINK: RoleSessionLink = {
  branchId: "branch_1",
  role: "developer",
  sessionId: "conv_dev",
  temporary: false,
  taskId: null,
  createdAt: NOW - 600,
  retiredAt: null,
  handoffFromSessionId: null,
};

function input(overrides: Partial<DiagnoseInput> = {}): DiagnoseInput {
  return {
    now: NOW,
    role: "developer",
    link: LINK,
    session: session("conv_dev"),
    failedDetail: null,
    lastTurnError: null,
    startFailure: null,
    awaiting: null,
    hostName: "laptop",
    harness: "claude-sdk",
    setupSteps: [],
    ...overrides,
  };
}

describe("diagnoseRole", () => {
  it("reports a failed session with Omnigent's own error, cause, and fix", () => {
    const { state, problem } = diagnoseRole(
      input({
        session: session("conv_dev", { status: "failed" }),
        failedDetail: { code: "budget_exhausted", message: "Monthly budget used", remediation: "Raise the budget" },
      }),
    );
    expect(state).toBe("problem");
    expect(problem).toMatchObject({ category: "usage_limit", detail: "Monthly budget used", remediation: "Raise the budget" });
    expect(problem?.actions[0]).toBe("switch_harness");
  });

  it("catches a turn that ended in an error while the session reads idle", () => {
    const { problem } = diagnoseRole(input({ lastTurnError: { code: "native_turn_error", message: "429 rate limit reached", at: NOW - 30 } }));
    expect(problem).toMatchObject({ category: "rate_limit", since: NOW - 30 });
    // The same error is ignored while a new turn runs.
    expect(diagnoseRole(input({ session: session("conv_dev", { status: "running" }), lastTurnError: { code: "x", message: "429" } })).state).toBe("working");
  });

  it("points a sign-in failure at the harness's own setup step", () => {
    const { problem } = diagnoseRole(
      input({
        harness: "codex",
        lastTurnError: { code: "codex_reauth_required", message: "Please log in" },
        setupSteps: [{ kind: "install", command: null }, { kind: "auth", title: "Set up authentication", command: "codex login" }],
      }),
    );
    expect(problem?.category).toBe("auth");
    expect(problem?.remediation).toBe("Set up authentication: run `codex login` on the host.");
  });

  it("shows host loss as offline and a missing session as gone, never as progress", () => {
    expect(diagnoseRole(input({ session: session("conv_dev", { host_online: false, status: "running" }) })).problem?.category).toBe("host_offline");
    expect(diagnoseRole(input({ session: null })).problem?.category).toBe("missing");
  });

  it("flags a turn with no new activity for too long", () => {
    const stuck = session("conv_dev", { status: "running", updated_at: NOW - RUNNING_STALL_SECONDS - 1 });
    expect(diagnoseRole(input({ session: stuck })).problem?.category).toBe("stalled");
    expect(diagnoseRole(input({ session: session("conv_dev", { status: "running", updated_at: NOW - 60 }) })).state).toBe("working");
  });

  it("notices a PM that went idle mid-interview", () => {
    const awaiting = { since: NOW - NO_RESPONSE_SECONDS - 10, reason: "The PM has not planned or asked any interview questions yet." };
    const pm = { ...LINK, role: "pm" as const, sessionId: "conv_pm" };
    const idle = session("conv_pm", { updated_at: NOW - 30 });
    expect(diagnoseRole(input({ role: "pm", link: pm, session: idle, awaiting })).problem).toMatchObject({ category: "no_response", actions: ["nudge", "restart", "switch_harness"] });
    // Not yet: the PM has not even picked up the kickoff message.
    expect(diagnoseRole(input({ role: "pm", link: pm, session: session("conv_pm", { updated_at: awaiting.since - 5 }), awaiting })).problem).toBeNull();
  });

  it("explains a role that never started and offers to start it", () => {
    const { problem } = diagnoseRole(
      input({
        link: null,
        session: null,
        startFailure: { branchId: "b", role: "developer", harness: "copilot", at: NOW, code: "harness_not_configured", message: "copilot is not configured" },
      }),
    );
    expect(problem).toMatchObject({ category: "start_failed", cause: "Harness not set up on the host", actions: ["start", "switch_harness"] });
    expect(diagnoseRole(input({ link: null, session: null })).state).toBe("not_started");
    expect(diagnoseRole(input({ role: "pm", link: null, session: null })).problem).toMatchObject({ category: "missing", actions: ["start", "switch_harness"] });
  });

  it("does not offer restart or harness switching for a temporary worker", () => {
    const { problem } = diagnoseRole(input({ link: { ...LINK, temporary: true }, lastTurnError: { code: "x", message: "something odd" } }));
    expect(problem?.actions).toEqual(["continue", "recover"]);
  });
});

describe("TroubleshootService", () => {
  let directory: string;
  let store: StateStore;
  let omnigent: FakeOmnigent;
  let workflow: WorkflowService;
  let service: TroubleshootService;
  let clock: number;
  let sessions: Map<string, OmnigentSessionListItem>;
  let scans: Map<string, SessionScan>;
  const sent: { sessionId: string; text: string }[] = [];
  const retried: string[] = [];
  const notifications: string[] = [];

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "office-troubleshoot-"));
    store = new StateStore(join(directory, "office-state.json"));
    omnigent = new FakeOmnigent();
    clock = NOW;
    sessions = new Map();
    scans = new Map();
    sent.length = 0;
    retried.length = 0;
    notifications.length = 0;
    workflow = new WorkflowService({ omnigent, store, notifier: { notify: () => undefined }, mcpUrl: "http://127.0.0.1:1/office/mcp", clock: () => clock });
    const building = {
      getSnapshot: async () => ({ serverNow: clock, rooms: [{ projectId: "proj", name: "Shop" }], connection: { omnigentUrl: "", ok: true } }),
      session: (id: string) => sessions.get(id) ?? null,
      sessionScan: (id: string) => scans.get(id),
      sendMessage: async (sessionId: string, text: string) => {
        sent.push({ sessionId, text });
        return { queued: true };
      },
      invalidate: () => undefined,
    } as unknown as BuildingService;
    const client = {
      getSession: async (id: string) => ({ id, last_task_error: { code: "runner_disconnected", message: "The runner went away" } }),
      retrySession: async (id: string) => {
        retried.push(id);
        return { recovered: true, recovery: "runner_relaunched" };
      },
      interrupt: async () => undefined,
      listHosts: async () => ({ hosts: [] }),
      listHarnesses: async () => ({ setup_steps: {} }),
    } as unknown as OmnigentClient;
    service = new TroubleshootService({ building, workflow, client, store, notifier: { notify: (title) => notifications.push(title) }, clock: () => clock });
  });

  afterEach(() => rmSync(directory, { recursive: true, force: true }));

  async function pmBranch() {
    const view = await workflow.assignTheme("proj", { runtime: { harness: "claude-sdk", runnerId: "runner_1" } });
    const pm = view.roleSessions[0].sessionId;
    sessions.set(pm, session(pm));
    return { branchId: view.branch.id, pm };
  }

  const pmHealth = async (branchId: string) => (await service.health(branchId)).find((entry) => entry.role === "pm");

  it("diagnoses, flags the room, and notifies once per problem", async () => {
    const { branchId, pm } = await pmBranch();
    expect((await pmHealth(branchId))?.state).toBe("idle");
    expect(service.problemCount("proj")).toBe(0);
    sessions.set(pm, session(pm, { status: "failed" }));
    const health = await pmHealth(branchId);
    expect(health?.problem).toMatchObject({ category: "runner", detail: "The runner went away" });
    expect(service.problemCount("proj")).toBe(1);
    await service.health(branchId);
    expect(notifications).toEqual(["Shop: PM needs help"]);
  });

  it("continues a turn that died on a rate limit and records the action", async () => {
    const { branchId, pm } = await pmBranch();
    scans.set(pm, { scannedUpdatedAt: 0, latestHumanItemId: null, latestHumanAt: null, complete: true, lastTurnError: { code: "rate_limit_exceeded", message: "429" } });
    const { result } = await service.act(branchId, pm, "continue");
    expect(result).toBe("Asked the agent to continue its turn.");
    expect(sent).toEqual([{ sessionId: pm, text: CONTINUE_TEXT }]);
    expect(workflow.view(branchId).events[0].summary).toContain('"Continue the turn" for the PM');
    sessions.set(pm, session(pm, { status: "running" }));
    await expect(service.act(branchId, pm, "continue")).rejects.toMatchObject({ code: "turn_running" });
    await expect(service.act(branchId, pm, "restart")).rejects.toMatchObject({ code: "invalid_action" });
  });

  it("keeps retrying on a schedule until the role recovers, with the browser closed", async () => {
    const { branchId, pm } = await pmBranch();
    sessions.set(pm, session(pm, { status: "failed" }));
    await service.scheduleRetry(branchId, pm, 5, true);
    await service.tick();
    expect(retried).toEqual([]);
    clock += 5 * 60;
    await service.tick();
    // A lost runner is reconnected rather than sent another message.
    expect(retried).toEqual([pm]);
    expect(store.state.troubleshooting.retries[pm]).toMatchObject({ attempts: 1, nextAt: clock + 300 });
    sessions.set(pm, session(pm));
    clock += 5 * 60;
    await service.tick();
    expect(retried).toEqual([pm]);
    expect(store.state.troubleshooting.retries[pm]).toBeUndefined();
    expect(workflow.view(branchId).events[0].summary).toContain("recovered");
  });

  it("lets the PO ignore one occurrence without hiding a new failure", async () => {
    const { branchId, pm } = await pmBranch();
    sessions.set(pm, session(pm, { status: "failed" }));
    const first = (await pmHealth(branchId))?.problem;
    await expect(service.dismiss(branchId, pm, "stale")).rejects.toMatchObject({ code: "stale_request" });
    await service.dismiss(branchId, pm, first?.key);
    expect((await pmHealth(branchId))?.dismissed).toBe(true);
    expect(service.problemCount("proj")).toBe(0);
    sessions.set(pm, session(pm, { status: "failed", updated_at: NOW + 100 }));
    const again = await pmHealth(branchId);
    expect(again?.problem?.key).not.toBe(first?.key);
    expect(again?.dismissed).toBe(false);
    expect(service.problemCount("proj")).toBe(1);
  });
});
