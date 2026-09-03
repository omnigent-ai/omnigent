import { beforeEach, describe, expect, it, vi } from "vitest";

const { authenticatedFetchMock, createSessionMock, updateSessionMock, bindMock, launchMock } =
  vi.hoisted(() => ({
    authenticatedFetchMock: vi.fn(),
    createSessionMock: vi.fn(),
    updateSessionMock: vi.fn(),
    bindMock: vi.fn(),
    launchMock: vi.fn(),
  }));

vi.mock("@/lib/identity", () => ({ authenticatedFetch: authenticatedFetchMock }));
vi.mock("@/lib/sessionsApi", () => ({
  createSession: createSessionMock,
  updateSession: updateSessionMock,
  bindOnlyOnlineRunner: bindMock,
  launchRunner: launchMock,
}));

import {
  buildContributorIntroMessage,
  buildOfficerRelayMessage,
  buildRequesterAgentMessage,
  createDpiaContributorSession,
  createDpiaRequestSession,
  displayMessageFromWireText,
  listDpiaSessionsByRole,
} from "./requestSession";

beforeEach(() => {
  vi.clearAllMocks();
  createSessionMock.mockResolvedValue({ id: "session-1", runnerId: null });
  updateSessionMock.mockResolvedValue({ id: "session-1", runnerId: null });
  bindMock.mockResolvedValue({ id: "session-1" });
  launchMock.mockResolvedValue({ runnerId: "runner-1" });
});

describe("listDpiaSessionsByRole", () => {
  it("filters sessions by product and role labels", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          data: [
            {
              id: "s-req",
              labels: {
                "omnigent.product": "dpia-investigation",
                "omnigent.dpia.role": "requester",
              },
            },
            {
              id: "s-case",
              labels: {
                "omnigent.product": "dpia-investigation",
                "omnigent.case_id": "student-success-alert",
              },
            },
            { id: "s-other", labels: { "omnigent.product": "other" } },
          ],
        }),
    });
    const sessions = await listDpiaSessionsByRole("requester");
    expect(sessions).toEqual([
      {
        sessionId: "s-req",
        labels: {
          "omnigent.product": "dpia-investigation",
          "omnigent.dpia.role": "requester",
        },
      },
    ]);
  });

  it("throws on a failed listing", async () => {
    authenticatedFetchMock.mockResolvedValue({ ok: false, status: 500 });
    await expect(listDpiaSessionsByRole("contributor")).rejects.toThrow(/500/);
  });
});

describe("session creation", () => {
  it("creates a draft requester session with role labels and binds a runner", async () => {
    const session = await createDpiaRequestSession("agent-1");
    expect(createSessionMock).toHaveBeenCalledWith("agent-1", [], {
      title: "DPIA request (draft)",
    });
    expect(updateSessionMock).toHaveBeenCalledWith("session-1", {
      labels: {
        "omnigent.product": "dpia-investigation",
        "omnigent.dpia.role": "requester",
        "omnigent.dpia.request_status": "draft",
      },
    });
    expect(bindMock).toHaveBeenCalledWith("session-1");
    expect(session.sessionId).toBe("session-1");
  });

  it("creates a contributor session scoped to a case", async () => {
    await createDpiaContributorSession("agent-1", {
      caseId: "student-success-alert",
      contributor: "IT Security",
      requestId: "req-vendor-abc",
    });
    expect(updateSessionMock).toHaveBeenCalledWith("session-1", {
      labels: {
        "omnigent.product": "dpia-investigation",
        "omnigent.dpia.role": "contributor",
        "omnigent.case_id": "student-success-alert",
        "omnigent.dpia.contributor": "IT Security",
        "omnigent.dpia.response_status": "draft",
        "omnigent.dpia.request_id": "req-vendor-abc",
      },
    });
  });

  it("launches a runner from an existing session's host and workspace when none binds", async () => {
    bindMock.mockResolvedValue(null);
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          data: [
            { id: "s-old", agent_name: "other-agent" },
            {
              id: "s-template",
              agent_name: "dpia-investigation",
              workspace: "/repo/worktree",
              host_id: "host-1",
            },
          ],
        }),
    });
    await createDpiaRequestSession("agent-1");
    expect(launchMock).toHaveBeenCalledWith("host-1", "session-1", "/repo/worktree");
  });
});

describe("role message wrapping", () => {
  it("round-trips requester and officer messages", () => {
    const wire = buildRequesterAgentMessage("I need a DPIA for a new vendor.");
    expect(displayMessageFromWireText(wire)).toEqual({
      text: "I need a DPIA for a new vendor.",
      sender: "participant",
    });
    const officerWire = buildOfficerRelayMessage("Please clarify the hosting region.");
    expect(displayMessageFromWireText(officerWire)).toEqual({
      text: "Please clarify the hosting region.",
      sender: "officer",
    });
    expect(displayMessageFromWireText("plain text")).toEqual({
      text: "plain text",
      sender: "participant",
    });
  });

  it("builds a contributor intro carrying scoped questions and the artifact contract", () => {
    const intro = buildContributorIntroMessage({
      caseId: "student-success-alert",
      caseTitle: "Student Success Alert",
      contributor: "IT Security",
      requestId: "req-vendor-abc",
      questions: [{ id: "q-hosting", text: "Where are the model and database hosted?" }],
    });
    expect(intro).toContain('"question_id":"q-hosting"');
    expect(intro).toContain("stakeholder-response");
    expect(intro).toContain('request_id "req-vendor-abc"');
    expect(displayMessageFromWireText(intro).sender).toBe("officer");
  });
});
