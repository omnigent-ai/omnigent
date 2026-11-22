import { afterEach, expect, it, vi } from "vitest";
import {
  DEFAULT_TICKET_STAGES,
  createTicket,
  getTicket,
  getTicketSummary,
  getTicketStages,
  listTickets,
  setTicketSessionInvolvement,
  ticketEventStage,
  updateTicket,
  updateTicketStages,
  type TicketEvent,
} from "./projectTicketsApi";

afterEach(() => vi.unstubAllGlobals());

const event: TicketEvent = {
  id: "event",
  ticket_id: "ticket",
  position: 0,
  kind: "updated",
  user_id: null,
  session_id: null,
  agent_id: null,
  payload: {},
  created_at: 1000,
};

it("reads the project-wide response summary without fetching board pages", async () => {
  const summary = { needs_response_count: 4, is_complete: true };
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(summary)));
  vi.stubGlobal("fetch", fetchMock);
  expect(await getTicketSummary("project one")).toEqual(summary);
  expect(fetchMock).toHaveBeenCalledOnce();
  expect(fetchMock).toHaveBeenCalledWith(
    "/v1/projects/project%20one/tickets/summary",
    expect.objectContaining({ method: "GET" }),
  );
});

it.each([{ labels: [] }, { labels: [{ key: "needs-human", value: "approval" }, { key: "area" }] }])(
  "uses labels for ticket creation and replacement: %j",
  async ({ labels }) => {
    const ticket = { id: "ticket", stage_id: "open", labels };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(ticket)));
    vi.stubGlobal("fetch", fetchMock);
    expect(await createTicket("project", { title: "Work", labels })).toEqual(ticket);
    expect(await updateTicket("project", "ticket", { labels })).toEqual(ticket);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/v1/projects/project/tickets",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ title: "Work", labels }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/v1/projects/project/tickets/ticket",
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ labels }) }),
    );
  },
);

it.each(["Planning the approach", ""])(
  "sends involvement, including an explicit clear (%s)",
  async (involvement) => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ id: "ticket", stage_id: "investigating" })));
    vi.stubGlobal("fetch", fetchMock);
    expect(
      await setTicketSessionInvolvement("project one", "ticket", "worker", involvement),
    ).toEqual({ id: "ticket", stage_id: "investigating" });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/project%20one/tickets/ticket/sessions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      session_id: "worker",
      action: "attach",
      involvement,
    });
  },
);

it("reads real stage definitions and saves configuration with removal destinations", async () => {
  const configuration = {
    ...DEFAULT_TICKET_STAGES,
    stages: DEFAULT_TICKET_STAGES.stages.map((stage) => ({
      ...stage,
      color: "purple" as const,
      icon: "search" as const,
    })),
    occupied_stage_ids: ["planning"],
  };
  const fetchMock = vi
    .fn()
    .mockImplementation(async () => new Response(JSON.stringify(configuration)));
  vi.stubGlobal("fetch", fetchMock);
  expect(await getTicketStages("project one", true)).toEqual(configuration);
  const changes = {
    stages: configuration.stages.filter((stage) => stage.id !== "planning"),
    initial_stage_id: "in_progress",
    moves: { planning: "in_progress" },
  };
  await updateTicketStages("project one", changes);
  expect(fetchMock.mock.calls[0][0]).toBe(
    "/v1/projects/project%20one/tickets/stages?include_usage=true",
  );
  const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
  expect(url).toBe("/v1/projects/project%20one/tickets/stages");
  expect(init.method).toBe("PUT");
  expect(JSON.parse(init.body as string)).toEqual(changes);
});

it.each([false, true])(
  "adapts list and update stage fields only for older servers (legacy=%s)",
  async (legacy) => {
    const wireTicket = {
      id: "ticket",
      ...(legacy ? { status: "planning" } : { stage_id: "investigating" }),
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ data: [wireTicket], has_more: false, last_id: "ticket" })),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify(wireTicket)))
      .mockResolvedValueOnce(new Response(JSON.stringify(wireTicket)));
    vi.stubGlobal("fetch", fetchMock);
    const stageId = legacy ? "planning" : "investigating";
    expect(
      (await listTickets("project", { stage_id: stageId, after: "previous", legacy })).data[0]
        .stage_id,
    ).toBe(stageId);
    expect(fetchMock.mock.calls[0][0]).toBe(
      `/v1/projects/project/tickets?limit=50&${legacy ? "status" : "stage_id"}=${stageId}&after=previous`,
    );
    expect(
      (
        await updateTicket(
          "project",
          "ticket",
          { stage_id: stageId, description: "Saved approach" },
          legacy,
        )
      ).stage_id,
    ).toBe(stageId);
    const [, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      [legacy ? "status" : "stage_id"]: stageId,
      description: "Saved approach",
    });
    expect((await getTicket("project", "ticket")).stage_id).toBe(stageId);
  },
);

it("rejects a ticket without any stage instead of silently assigning a default", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "ticket" }))));
  await expect(getTicket("project", "ticket")).rejects.toThrow("without a stage");
});

it.each([
  {
    payload: {
      stage: { id: "removed", name: "Former stage", is_complete: true },
      changes: { stage_id: "removed" },
    },
    name: "Former stage",
    complete: true,
  },
  { payload: { changes: { status: "done" } }, name: "Done", complete: true },
  { payload: { changes: { stage_id: "unknown" } }, name: "unknown", complete: false },
])("reads immutable or legacy stage history: $name", ({ payload, name, complete }) => {
  expect(ticketEventStage({ ...event, payload })).toMatchObject({
    name,
    is_complete: complete,
  });
});

it("does not invent a stage for unrelated activity", () => {
  expect(
    ticketEventStage({ ...event, payload: { changes: { plan: "Review the design" } } }),
  ).toBeNull();
});
