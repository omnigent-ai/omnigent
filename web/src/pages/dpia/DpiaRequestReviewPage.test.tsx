import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as SessionChatModule from "./useDpiaSessionChat";
import type * as RequestSessionModule from "@/lib/dpia/requestSession";
import type * as DpiaApiModule from "@/lib/dpia/dpiaApi";

const { chatMock, requestsMock, labelsMock, postEventMock, recordEventMock } = vi.hoisted(() => ({
  chatMock: vi.fn(),
  requestsMock: vi.fn(),
  labelsMock: vi.fn(),
  postEventMock: vi.fn(),
  recordEventMock: vi.fn(),
}));

vi.mock("./useDpiaSessionChat", async (importActual) => ({
  ...(await importActual<typeof SessionChatModule>()),
  useDpiaSessionChat: chatMock,
}));
vi.mock("@/hooks/useDpiaRequests", () => ({ useDpiaRequests: requestsMock }));
vi.mock("@/lib/dpia/requestSession", async (importActual) => ({
  ...(await importActual<typeof RequestSessionModule>()),
  setDpiaSessionLabels: labelsMock,
}));
vi.mock("@/lib/sessionsApi", () => ({ postEvent: postEventMock }));
vi.mock("@/lib/dpia/dpiaApi", async (importActual) => ({
  ...(await importActual<typeof DpiaApiModule>()),
  recordDpiaCaseEvent: recordEventMock,
}));

import { DpiaRequestReviewPage } from "./DpiaRequestReviewPage";

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
  known_unknowns: ["Hosting location"],
  submitted_at: "2026-08-22T09:00:00.000Z",
};

function summaryStub(status = "submitted") {
  return {
    sessionId: "s-req",
    requestId: "req-vendor-abc",
    status,
    acknowledged: false,
    caseId: null,
    request,
    outcome: null,
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/dpia/requests/req-vendor-abc"]}>
      <Routes>
        <Route path="/dpia/requests/:requestId" element={<DpiaRequestReviewPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  chatMock.mockReturnValue({
    historyBubbles: [],
    liveBubbles: [],
    localMessages: [],
    streamState: "connected",
    chatError: null,
    sending: false,
    sendMessage: vi.fn(),
    retry: vi.fn(),
  });
  requestsMock.mockReturnValue({ data: [summaryStub()], isLoading: false, isError: false });
  labelsMock.mockResolvedValue(undefined);
  postEventMock.mockResolvedValue({ denied: false });
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DpiaRequestReviewPage", () => {
  it("shows the normalized request and accepts it for screening", async () => {
    renderPage();
    const card = screen.getByTestId("dpia-request-detail-card");
    expect(card).toHaveTextContent("Vendor Wellbeing Analytics");
    expect(card).toHaveTextContent("Priya Shah (Procurement)");
    expect(card).toHaveTextContent("Hosting location");

    fireEvent.click(screen.getByRole("button", { name: "Accept for screening" }));
    await waitFor(() =>
      expect(labelsMock).toHaveBeenCalledWith("s-req", {
        "omnigent.dpia.request_status": "accepted",
        "omnigent.case_id": "student-success-alert",
      }),
    );
    expect(recordEventMock).toHaveBeenCalledWith(
      "student-success-alert",
      expect.objectContaining({ action: "Accepted DPIA request for screening" }),
    );
    expect(screen.getByRole("button", { name: "Send outcome to requester" })).toBeInTheDocument();
  });

  it("declines at triage by publishing a not-required outcome", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Decline — DPIA not required" }));
    fireEvent.change(await screen.findByLabelText(/Reason/), {
      target: { value: "The processing involves no personal data at all." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Record decline" }));

    await waitFor(() => expect(postEventMock).toHaveBeenCalled());
    const [, event] = postEventMock.mock.calls[0] as [
      string,
      { data: { content: { text: string }[] } },
    ];
    const outcome = JSON.parse(event.data.content[0].text) as {
      artifact: string;
      decision: string;
    };
    expect(outcome.artifact).toBe("dpia-outcome");
    expect(outcome.decision).toBe("not-required");
    await waitFor(() =>
      expect(labelsMock).toHaveBeenCalledWith("s-req", {
        "omnigent.dpia.request_status": "declined",
      }),
    );
  });

  it("sends a clarification into the requester conversation as the Privacy Office", async () => {
    renderPage();
    fireEvent.change(screen.getByLabelText(/Ask the requester for clarification/), {
      target: { value: "Which team owns the vendor contract?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send clarification" }));

    await waitFor(() => expect(postEventMock).toHaveBeenCalled());
    const [, event] = postEventMock.mock.calls[0] as [
      string,
      { data: { content: { text: string }[] } },
    ];
    expect(event.data.content[0].text).toContain("DPIA officer message follows:");
    expect(event.data.content[0].text).toContain("Which team owns the vendor contract?");
  });
});
