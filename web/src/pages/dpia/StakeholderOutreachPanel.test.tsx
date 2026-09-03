import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createStudentSuccessAlertSeed } from "@/lib/dpia/seed";
import type * as RequestSessionModule from "@/lib/dpia/requestSession";
import type * as DpiaApiModule from "@/lib/dpia/dpiaApi";

const {
  responsesMock,
  refetchMock,
  createContributorMock,
  labelsMock,
  postEventMock,
  recordEventMock,
} = vi.hoisted(() => ({
  responsesMock: vi.fn(),
  refetchMock: vi.fn(),
  createContributorMock: vi.fn(),
  labelsMock: vi.fn(),
  postEventMock: vi.fn(),
  recordEventMock: vi.fn(),
}));

vi.mock("@/hooks/useDpiaRequests", () => ({ useDpiaContributorResponses: responsesMock }));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [{ id: "agent-1", name: "dpia-investigation" }],
    isLoading: false,
  }),
}));
vi.mock("@/lib/dpia/requestSession", async (importActual) => ({
  ...(await importActual<typeof RequestSessionModule>()),
  createDpiaContributorSession: createContributorMock,
  setDpiaSessionLabels: labelsMock,
}));
vi.mock("@/lib/sessionsApi", () => ({ postEvent: postEventMock }));
vi.mock("@/lib/dpia/dpiaApi", async (importActual) => ({
  ...(await importActual<typeof DpiaApiModule>()),
  recordDpiaCaseEvent: recordEventMock,
}));

import { StakeholderOutreachPanel } from "./StakeholderOutreachPanel";

const caseData = createStudentSuccessAlertSeed();

function contributorRow() {
  return {
    sessionId: "s-contrib",
    contributor: "IT Security",
    caseId: caseData.id,
    requestId: null,
    status: "submitted" as const,
    response: {
      artifact: "stakeholder-response" as const,
      case_id: caseData.id,
      respondent: { name: "Jordan Ali", team: "IT Security" },
      answers: [
        {
          question_id: caseData.questions[0].id,
          response: "The model and primary database are hosted in London.",
        },
      ],
      submitted_at: "2026-08-22T10:00:00.000Z",
    },
  };
}

function renderPanel(onAcceptAnswer = vi.fn()) {
  render(
    <MemoryRouter>
      <StakeholderOutreachPanel caseData={caseData} onAcceptAnswer={onAcceptAnswer} />
    </MemoryRouter>,
  );
  return onAcceptAnswer;
}

beforeEach(() => {
  responsesMock.mockReturnValue({ data: [], refetch: refetchMock });
  refetchMock.mockResolvedValue(undefined);
  createContributorMock.mockResolvedValue({ sessionId: "s-new", labels: {} });
  labelsMock.mockResolvedValue(undefined);
  postEventMock.mockResolvedValue({ denied: false });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("StakeholderOutreachPanel", () => {
  it("creates a scoped contributor session with the officer intro and audit event", async () => {
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Share questions with a stakeholder" }));
    fireEvent.change(await screen.findByLabelText("Stakeholder team"), {
      target: { value: "IT Security" },
    });
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    fireEvent.click(screen.getByRole("button", { name: "Create scoped outreach" }));

    await waitFor(() => expect(createContributorMock).toHaveBeenCalled());
    expect(createContributorMock).toHaveBeenCalledWith("agent-1", {
      caseId: caseData.id,
      contributor: "IT Security",
    });
    const [, event] = postEventMock.mock.calls[0] as [
      string,
      { data: { content: { text: string }[] } },
    ];
    expect(event.data.content[0].text).toContain("Scoped questions JSON:");
    expect(recordEventMock).toHaveBeenCalledWith(
      caseData.id,
      expect.objectContaining({ action: "Shared scoped questions with stakeholder" }),
    );
    expect(await screen.findByText("/dpia/respond/s-new")).toBeInTheDocument();
  });

  it("accepts a submitted response into recorded answers and confirms to the contributor", async () => {
    responsesMock.mockReturnValue({ data: [contributorRow()], refetch: refetchMock });
    const onAcceptAnswer = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "Accept as recorded answers" }));
    await waitFor(() =>
      expect(labelsMock).toHaveBeenCalledWith("s-contrib", {
        "omnigent.dpia.response_status": "accepted",
      }),
    );
    expect(onAcceptAnswer).toHaveBeenCalledWith({
      questionId: caseData.questions[0].id,
      response: "The model and primary database are hosted in London.",
      answeredBy: "Jordan Ali (IT Security)",
    });
    const [, event] = postEventMock.mock.calls[0] as [
      string,
      { data: { content: { text: string }[] } },
    ];
    expect(event.data.content[0].text).toContain("accepted and recorded");
  });

  it("rejects a submitted response with an officer note", async () => {
    responsesMock.mockReturnValue({ data: [contributorRow()], refetch: refetchMock });
    const onAcceptAnswer = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() =>
      expect(labelsMock).toHaveBeenCalledWith("s-contrib", {
        "omnigent.dpia.response_status": "rejected",
      }),
    );
    expect(onAcceptAnswer).not.toHaveBeenCalled();
  });
});
