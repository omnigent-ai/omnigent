import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { BuildingService, OfficeRequestError } from "./building.ts";
import type { ItemsPage } from "./activity.ts";
import type { OmnigentClient, OmnigentProject, OmnigentSessionListItem } from "./omnigentClient.ts";
import { StateStore } from "./stateStore.ts";

const NOW = 1_800_000_000;

function session(id: string, overrides: Partial<OmnigentSessionListItem> = {}): OmnigentSessionListItem {
  return {
    id,
    title: id,
    agent_name: "claude",
    status: "idle",
    project_id: "proj",
    parent_session_id: null,
    created_at: NOW - 100,
    updated_at: NOW - 50,
    host_online: true,
    runner_online: true,
    pending_elicitations_count: 0,
    archived: false,
    ...overrides,
  };
}

class FakeClient {
  baseUrl = "http://omnigent.test";
  projects: OmnigentProject[] = [{ id: "proj", name: "Project", created_at: NOW - 1000 }];
  sessions: OmnigentSessionListItem[] = [session("conv_a")];
  items: Record<string, ItemsPage> = {
    conv_a: { data: [{ id: "item_1", type: "message", role: "user", created_at: NOW - 60 }], has_more: false },
  };
  pending: Record<string, { elicitation_id: string }[]> = {};
  resolved: { sessionId: string; elicitationId: string; action: string }[] = [];
  messages: { sessionId: string; text: string }[] = [];
  itemCalls = 0;

  async listProjects() {
    return this.projects;
  }
  async listSessions() {
    return this.sessions;
  }
  async listItems(sessionId: string) {
    this.itemCalls += 1;
    return this.items[sessionId] ?? { data: [], has_more: false };
  }
  async getSession(sessionId: string) {
    return { id: sessionId, project_id: "proj", pending_elicitations: this.pending[sessionId] ?? [] };
  }
  async resolveElicitation(sessionId: string, elicitationId: string, action: string) {
    this.resolved.push({ sessionId, elicitationId, action });
    this.pending[sessionId] = (this.pending[sessionId] ?? []).filter((request) => request.elicitation_id !== elicitationId);
    return { queued: false };
  }
  async sendMessage(sessionId: string, text: string) {
    this.messages.push({ sessionId, text });
    return { queued: true };
  }
  async createProject(name: string, config: OmnigentProject["config"]) {
    const project = { id: `proj_${this.projects.length}`, name, created_at: NOW, config };
    this.projects.push(project);
    return project;
  }
}

describe("BuildingService", () => {
  let directory: string;
  let client: FakeClient;
  let store: StateStore;
  let service: BuildingService;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "office-test-"));
    client = new FakeClient();
    store = new StateStore(join(directory, "state.json"));
    service = new BuildingService(client as unknown as OmnigentClient, store, () => NOW);
  });

  afterEach(() => rmSync(directory, { recursive: true, force: true }));

  it("summarizes rooms with their latest human interaction and persists first sync", async () => {
    const snapshot = await service.getSnapshot(true);
    expect(snapshot.rooms).toEqual([
      expect.objectContaining({
        projectId: "proj",
        latestInteractionAt: NOW - 60,
        firstSyncAt: NOW,
        historyComplete: true,
        workerCount: 1,
      }),
    ]);
    // A restarted service keeps the original first-sync time.
    const restarted = new BuildingService(client as unknown as OmnigentClient, new StateStore(store.path), () => NOW + 500);
    expect((await restarted.getSnapshot(true)).rooms[0].firstSyncAt).toBe(NOW);
  });

  it("rescans a session only after it changes", async () => {
    await service.getSnapshot(true);
    await service.getSnapshot(true);
    expect(client.itemCalls).toBe(1);
    client.sessions = [session("conv_a", { updated_at: NOW })];
    await service.getSnapshot(true);
    expect(client.itemCalls).toBe(2);
  });

  it("persists priority and Keep upstairs and rejects invalid values", async () => {
    await service.updateRoom("proj", { priority: "high", keepUpstairs: true });
    expect(new StateStore(store.path).state.rooms.proj).toMatchObject({ priority: "high", keepUpstairs: true });
    await expect(service.updateRoom("proj", { priority: "urgent" as never })).rejects.toBeInstanceOf(OfficeRequestError);
    await expect(service.updateRoom("missing", { priority: "high" })).rejects.toMatchObject({ status: 404 });
  });

  it("binds a decision to a pending request, rejects stale ones, and is idempotent on retry", async () => {
    client.pending.conv_a = [{ elicitation_id: "elicit_1" }];
    await service.getSnapshot(true);

    const first = await service.decide("conv_a", "elicit_1", "accept");
    expect(first.replayed).toBe(false);
    expect(client.resolved).toHaveLength(1);

    const retry = await service.decide("conv_a", "elicit_1", "accept");
    expect(retry.replayed).toBe(true);
    expect(client.resolved).toHaveLength(1);

    await expect(service.decide("conv_a", "elicit_1", "decline")).rejects.toMatchObject({ code: "already_decided" });
    await expect(service.decide("conv_a", "elicit_gone", "accept")).rejects.toMatchObject({ code: "stale_request" });
  });

  it("delivers feedback with a change request and counts the decision as an interaction", async () => {
    client.pending.conv_a = [{ elicitation_id: "elicit_2" }];
    client.items.conv_a = { data: [], has_more: false };
    await service.decide("conv_a", "elicit_2", "decline", "  Please split the migration.  ");
    expect(client.resolved[0].action).toBe("decline");
    expect(client.messages).toEqual([{ sessionId: "conv_a", text: "Please split the migration." }]);
    expect((await service.getSnapshot(true)).rooms[0].latestInteractionAt).toBe(NOW);
  });

  it("creates a room whose project config carries its host and folder", async () => {
    const { projectId } = await service.createRoom({ name: " Shop ", icon: "🛒", hostId: "host_1", workspace: "/repo/shop" });
    expect(client.projects.at(-1)?.config).toEqual({ icon: "🛒", host_id: "host_1", workspace: "/repo/shop" });
    const room = (await service.getSnapshot(true)).rooms.find((entry) => entry.projectId === projectId);
    expect(room).toMatchObject({ name: "Shop", hostId: "host_1", workspace: "/repo/shop" });
    await expect(service.createRoom({ name: "No host", workspace: "/repo" })).rejects.toMatchObject({ code: "invalid_workspace" });
    await expect(service.createRoom({ name: "No folder", hostId: "host_1" })).rejects.toMatchObject({ code: "invalid_workspace" });
  });

  it("keeps the last known building when Omnigent is unreachable", async () => {
    await service.getSnapshot(true);
    client.listProjects = async () => {
      throw new Error("connection refused");
    };
    const snapshot = await service.getSnapshot(true);
    expect(snapshot.connection).toMatchObject({ ok: false, error: "connection refused" });
    expect(snapshot.rooms).toHaveLength(1);
  });
});
