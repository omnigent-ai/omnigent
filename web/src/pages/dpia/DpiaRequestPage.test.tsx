import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as SessionChatModule from "./useDpiaSessionChat";
import type * as RequestSessionModule from "@/lib/dpia/requestSession";

const { chatMock, sendMessageMock, createSessionMock, listSessionsMock, labelsMock, renameMock } =
  vi.hoisted(() => ({
    chatMock: vi.fn(),
    sendMessageMock: vi.fn(),
    createSessionMock: vi.fn(),
    listSessionsMock: vi.fn(),
    labelsMock: vi.fn(),
    renameMock: vi.fn(),
  }));

vi.mock("./useDpiaSessionChat", async (importActual) => ({
  ...(await importActual<typeof SessionChatModule>()),
  useDpiaSessionChat: chatMock,
}));
vi.mock("@/lib/dpia/requestSession", async (importActual) => ({
  ...(await importActual<typeof RequestSessionModule>()),
  createDpiaRequestSession: createSessionMock,
  listDpiaSessionsByRole: listSessionsMock,
  setDpiaSessionLabels: labelsMock,
  renameDpiaSession: renameMock,
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [{ id: "agent-1", name: "dpia-investigation" }],
    isLoading: false,
  }),
}));

import { DpiaRequestPage } from "./DpiaRequestPage";

function chatStub() {
  return {
    historyBubbles: [],
    liveBubbles: [],
    localMessages: [],
    streamState: "connected" as const,
    chatError: null,
    sending: false,
    sendMessage: sendMessageMock,
    retry: vi.fn(),
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DpiaRequestPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  chatMock.mockReturnValue(chatStub());
  sendMessageMock.mockResolvedValue(true);
  createSessionMock.mockResolvedValue({ sessionId: "session-1", labels: {} });
  listSessionsMock.mockResolvedValue([]);
  labelsMock.mockResolvedValue(undefined);
  renameMock.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function startNewRequest() {
  renderPage();
  await waitFor(() => expect(screen.getByText("No DPIA requests yet.")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Start a new request" }));
  await waitFor(() => expect(screen.getByTestId("dpia-intake-card")).toBeInTheDocument());
}

function fillIntake() {
  fireEvent.change(screen.getByLabelText("Your name"), { target: { value: "Priya Shah" } });
  fireEvent.change(screen.getByLabelText("Your team"), { target: { value: "Procurement" } });
  fireEvent.change(screen.getByLabelText("Project title"), {
    target: { value: "Vendor Wellbeing Analytics" },
  });
  fireEvent.change(screen.getByLabelText("Purpose"), {
    target: { value: "Score student wellbeing surveys to prioritise support outreach." },
  });
  fireEvent.change(screen.getByLabelText("Data subjects"), {
    target: { value: "Enrolled students" },
  });
  fireEvent.change(screen.getByLabelText("Personal data involved"), {
    target: { value: "Survey responses, student ids" },
  });
  fireEvent.change(screen.getByLabelText("Vendors / processors"), {
    target: { value: "Acme Analytics Ltd" },
  });
  fireEvent.change(screen.getByLabelText("Timeline"), { target: { value: "Pilot in October" } });
}

describe("DpiaRequestPage", () => {
  it("starts a requester session and shows the intake card", async () => {
    await startNewRequest();
    expect(createSessionMock).toHaveBeenCalledWith("agent-1");
    expect(screen.getByTestId("dpia-request-transcript")).toBeInTheDocument();
  });

  it("submits the confirmed intake as a raw dpia-request artifact and labels the session", async () => {
    await startNewRequest();
    fillIntake();
    fireEvent.click(screen.getByRole("button", { name: "Review & submit" }));
    fireEvent.click(await screen.findByRole("button", { name: "Submit to DPIA Office" }));

    await waitFor(() => expect(sendMessageMock).toHaveBeenCalled());
    const [wireText] = sendMessageMock.mock.calls[0] as [string, string];
    const artifact = JSON.parse(wireText) as { artifact: string; request_id: string };
    expect(artifact.artifact).toBe("dpia-request");
    expect(artifact.request_id).toMatch(/^req-vendor-wellbeing/);
    await waitFor(() =>
      expect(labelsMock).toHaveBeenCalledWith("session-1", {
        "omnigent.dpia.request_id": artifact.request_id,
        "omnigent.dpia.request_status": "submitted",
      }),
    );
    expect(renameMock).toHaveBeenCalledWith(
      "session-1",
      "DPIA request: Vendor Wellbeing Analytics",
    );
    expect(screen.getByTestId("dpia-request-status")).toHaveTextContent("awaiting Privacy Office");
  });

  it("renders an officer outcome from the transcript and acknowledges it", async () => {
    const outcome = {
      artifact: "dpia-outcome",
      request_id: "req-vendor-abc",
      case_id: "student-success-alert",
      decision: "approved-with-conditions",
      reasons: ["Screening indicates a full DPIA is likely before launch."],
      conditions: [
        { action: "Confirm the hosting region.", owner: "Procurement", due: "2026-09-15" },
      ],
      review_date: "2027-02-01",
      contact: "privacy-office@university.example",
      decided_by: "Alex Morgan",
      decided_at: "2026-08-22T12:00:00.000Z",
    };
    chatMock.mockReturnValue({
      ...chatStub(),
      historyBubbles: [
        {
          kind: "user",
          itemId: "i-1",
          content: [{ type: "input_text", text: JSON.stringify(outcome) }],
        },
      ],
    });
    listSessionsMock.mockResolvedValue([
      {
        sessionId: "session-1",
        labels: {
          "omnigent.dpia.request_id": "req-vendor-abc",
          "omnigent.dpia.request_status": "completed",
        },
      },
    ]);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));

    const card = await screen.findByTestId("dpia-outcome-card");
    expect(card).toHaveTextContent("Approved with conditions");
    expect(card).toHaveTextContent("Confirm the hosting region.");

    fireEvent.click(screen.getByRole("button", { name: "Acknowledge outcome" }));
    await waitFor(() =>
      expect(labelsMock).toHaveBeenCalledWith("session-1", { "omnigent.dpia.ack": "true" }),
    );
  });
});
