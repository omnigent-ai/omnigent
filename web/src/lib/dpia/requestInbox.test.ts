import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as RequestSessionModule from "./requestSession";

const { listMock, fetchItemsMock } = vi.hoisted(() => ({
  listMock: vi.fn(),
  fetchItemsMock: vi.fn(),
}));

vi.mock("./requestSession", async (importActual) => ({
  ...(await importActual<typeof RequestSessionModule>()),
  listDpiaSessionsByRole: listMock,
}));
vi.mock("@/lib/sessionsApi", () => ({ fetchSessionItemsPage: fetchItemsMock }));

import { fetchDpiaContributorSummaries, fetchDpiaRequestSummaries } from "./requestInbox";

const request = {
  artifact: "dpia-request",
  request_id: "req-vendor-abc",
  requester: { name: "Priya Shah", team: "Procurement" },
  project: {
    title: "Vendor Wellbeing Analytics",
    purpose: "Score wellbeing surveys to prioritise support outreach.",
    data_subjects: "Students",
    personal_data: "Survey responses",
    vendors: "Acme",
    timeline: "October",
  },
  known_unknowns: [],
  submitted_at: "2026-08-22T09:00:00.000Z",
};

const response = {
  artifact: "stakeholder-response",
  case_id: "student-success-alert",
  respondent: { name: "Jordan Ali", team: "IT Security" },
  answers: [{ question_id: "q-hosting", response: "Hosted in London, UK region." }],
  submitted_at: "2026-08-22T10:00:00.000Z",
};

function userItem(text: string) {
  return {
    id: `item-${text.length}`,
    type: "message",
    role: "user",
    response_id: `resp-${text.length}`,
    content: [{ type: "input_text", text }],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("fetchDpiaRequestSummaries", () => {
  it("parses the latest request artifact from submitted requester sessions", async () => {
    listMock.mockResolvedValue([
      {
        sessionId: "s-draft",
        labels: { "omnigent.dpia.request_status": "draft" },
      },
      {
        sessionId: "s-req",
        labels: {
          "omnigent.dpia.request_id": "req-vendor-abc",
          "omnigent.dpia.request_status": "submitted",
        },
      },
    ]);
    fetchItemsMock.mockResolvedValue({
      items: [userItem("hello"), userItem(JSON.stringify(request))],
    });

    const summaries = await fetchDpiaRequestSummaries();
    expect(fetchItemsMock).toHaveBeenCalledTimes(1);
    expect(summaries).toHaveLength(1);
    expect(summaries[0].requestId).toBe("req-vendor-abc");
    expect(summaries[0].status).toBe("submitted");
    expect(summaries[0].request?.project.title).toBe("Vendor Wellbeing Analytics");
    expect(summaries[0].outcome).toBeNull();
  });
});

describe("fetchDpiaContributorSummaries", () => {
  it("returns contributor rows scoped to the case with parsed responses", async () => {
    listMock.mockResolvedValue([
      {
        sessionId: "s-contrib",
        labels: {
          "omnigent.case_id": "student-success-alert",
          "omnigent.dpia.contributor": "IT Security",
          "omnigent.dpia.response_status": "submitted",
        },
      },
      {
        sessionId: "s-other-case",
        labels: { "omnigent.case_id": "another-case" },
      },
    ]);
    fetchItemsMock.mockResolvedValue({ items: [userItem(JSON.stringify(response))] });

    const rows = await fetchDpiaContributorSummaries("student-success-alert");
    expect(rows).toHaveLength(1);
    expect(rows[0].contributor).toBe("IT Security");
    expect(rows[0].status).toBe("submitted");
    expect(rows[0].response?.answers[0].question_id).toBe("q-hosting");
  });
});
