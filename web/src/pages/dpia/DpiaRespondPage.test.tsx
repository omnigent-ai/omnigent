import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildContributorIntroMessage } from "@/lib/dpia/requestSession";
import type * as SessionChatModule from "./useDpiaSessionChat";
import type * as RequestSessionModule from "@/lib/dpia/requestSession";

const { chatMock, sendMessageMock, labelsMock } = vi.hoisted(() => ({
  chatMock: vi.fn(),
  sendMessageMock: vi.fn(),
  labelsMock: vi.fn(),
}));

vi.mock("./useDpiaSessionChat", async (importActual) => ({
  ...(await importActual<typeof SessionChatModule>()),
  useDpiaSessionChat: chatMock,
}));
vi.mock("@/lib/dpia/requestSession", async (importActual) => ({
  ...(await importActual<typeof RequestSessionModule>()),
  setDpiaSessionLabels: labelsMock,
}));

import { DpiaRespondPage } from "./DpiaRespondPage";

const intro = buildContributorIntroMessage({
  caseId: "student-success-alert",
  caseTitle: "Student Success Alert",
  contributor: "IT Security",
  requestId: "req-vendor-abc",
  questions: [
    { id: "q-hosting", text: "Where are the model and database hosted?" },
    { id: "q-access", text: "Who has access to the scores?" },
  ],
});

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/dpia/respond/session-9"]}>
      <Routes>
        <Route path="/dpia/respond/:sessionId" element={<DpiaRespondPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  chatMock.mockReturnValue({
    historyBubbles: [
      { kind: "user", itemId: "i-1", content: [{ type: "input_text", text: intro }] },
    ],
    liveBubbles: [],
    localMessages: [],
    streamState: "connected",
    chatError: null,
    sending: false,
    sendMessage: sendMessageMock,
    retry: vi.fn(),
  });
  sendMessageMock.mockResolvedValue(true);
  labelsMock.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DpiaRespondPage", () => {
  it("renders the scoped questions parsed from the officer intro", () => {
    renderPage();
    const card = screen.getByTestId("dpia-respond-card");
    expect(card).toHaveTextContent("Where are the model and database hosted?");
    expect(card).toHaveTextContent("Who has access to the scores?");
    expect(screen.getByRole("heading", { name: "Answer the Privacy Office" })).toBeInTheDocument();
  });

  it("submits confirmed answers as a raw stakeholder-response artifact and labels the session", async () => {
    renderPage();
    fireEvent.change(screen.getByLabelText("Your name"), { target: { value: "Jordan Ali" } });
    fireEvent.change(screen.getByLabelText("Your team"), { target: { value: "IT Security" } });
    fireEvent.change(screen.getByLabelText("Where are the model and database hosted?"), {
      target: { value: "The model and primary database are hosted in London." },
    });
    fireEvent.change(screen.getByLabelText("Who has access to the scores?"), {
      target: { value: "Only the student support triage team has access." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Review & submit answers" }));
    fireEvent.click(await screen.findByRole("button", { name: "Submit answers" }));

    await waitFor(() => expect(sendMessageMock).toHaveBeenCalled());
    const [wireText] = sendMessageMock.mock.calls[0] as [string, string];
    const artifact = JSON.parse(wireText) as {
      artifact: string;
      case_id: string;
      request_id?: string;
      answers: { question_id: string }[];
    };
    expect(artifact.artifact).toBe("stakeholder-response");
    expect(artifact.case_id).toBe("student-success-alert");
    expect(artifact.request_id).toBe("req-vendor-abc");
    expect(artifact.answers.map(({ question_id }) => question_id)).toEqual([
      "q-hosting",
      "q-access",
    ]);
    await waitFor(() =>
      expect(labelsMock).toHaveBeenCalledWith("session-9", {
        "omnigent.dpia.response_status": "submitted",
      }),
    );
  });
});
