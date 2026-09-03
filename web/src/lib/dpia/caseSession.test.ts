import { afterEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { bindOnlyOnlineRunner, createSession, updateSession } from "@/lib/sessionsApi";
import { findOrCreateDpiaCaseSession } from "./caseSession";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({
  bindOnlyOnlineRunner: vi.fn(),
  createSession: vi.fn(),
  updateSession: vi.fn(),
}));

afterEach(() => {
  vi.clearAllMocks();
});

describe("DPIA case session binding", () => {
  it("reuses a session carrying both case labels", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          data: [
            {
              id: "conv_dpia",
              runner_id: "runner_dpia",
              status: "idle",
              labels: {
                "omnigent.product": "dpia-investigation",
                "omnigent.case_id": "student-success-alert",
              },
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(
      findOrCreateDpiaCaseSession("student-success-alert", "agent_dpia"),
    ).resolves.toEqual({ sessionId: "conv_dpia", status: "idle", created: false });
    expect(createSession).not.toHaveBeenCalled();
    expect(updateSession).not.toHaveBeenCalled();
    expect(bindOnlyOnlineRunner).not.toHaveBeenCalled();
  });

  it("creates and labels a session when none is bound", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ data: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.mocked(createSession).mockResolvedValue({ id: "conv_new" } as never);
    vi.mocked(updateSession).mockResolvedValue({
      id: "conv_new",
      runnerId: null,
      status: "idle",
    } as never);
    vi.mocked(bindOnlyOnlineRunner).mockResolvedValue({
      id: "conv_new",
      runnerId: "runner_dpia",
      status: "idle",
    } as never);

    await expect(
      findOrCreateDpiaCaseSession("student-success-alert", "agent_dpia"),
    ).resolves.toEqual({ sessionId: "conv_new", status: "idle", created: true });
    expect(createSession).toHaveBeenCalledWith("agent_dpia", [], {
      title: "DPIA: student-success-alert",
    });
    expect(updateSession).toHaveBeenCalledWith("conv_new", {
      labels: {
        "omnigent.product": "dpia-investigation",
        "omnigent.case_id": "student-success-alert",
      },
    });
    expect(bindOnlyOnlineRunner).toHaveBeenCalledWith("conv_new");
  });

  it("repairs an existing labelled session that has no runner", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          data: [
            {
              id: "conv_unbound",
              runner_id: null,
              status: "idle",
              labels: {
                "omnigent.product": "dpia-investigation",
                "omnigent.case_id": "student-success-alert",
              },
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.mocked(bindOnlyOnlineRunner).mockResolvedValue({
      id: "conv_unbound",
      runnerId: "runner_dpia",
      status: "idle",
    } as never);

    await expect(
      findOrCreateDpiaCaseSession("student-success-alert", "agent_dpia"),
    ).resolves.toEqual({ sessionId: "conv_unbound", status: "idle", created: false });
    expect(bindOnlyOnlineRunner).toHaveBeenCalledWith("conv_unbound");
    expect(createSession).not.toHaveBeenCalled();
  });
});
