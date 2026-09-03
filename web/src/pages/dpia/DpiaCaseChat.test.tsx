import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { StreamEvent } from "@/lib/events";
import {
  approve,
  bindOnlyOnlineRunner,
  fetchSessionItemsPage,
  getSession,
  openSessionStream,
  postEvent,
} from "@/lib/sessionsApi";
import { parseSseStream } from "@/lib/sse";
import { DpiaCaseChat } from "./DpiaCaseChat";
import { buildDpiaAgentMessage } from "@/lib/dpia/agentContext";
import { createStudentSuccessAlertSeed } from "@/lib/dpia/seed";

vi.mock("@/components/blocks/BlockRenderer", () => ({
  FilePathAwareMessageResponse: ({ children }: { children: React.ReactNode }) => (
    <span>{children}</span>
  ),
}));
vi.mock("@/components/blocks/ApprovalCard", () => ({
  ApprovalCard: ({
    message,
    status,
    onSubmit,
    elicitationId,
  }: {
    message: string;
    status: string;
    elicitationId: string;
    onSubmit: (id: string, action: "accept") => void;
  }) => (
    <div>
      <span>{message}</span>
      <span>{status}</span>
      <button type="button" onClick={() => onSubmit(elicitationId, "accept")}>
        Approve
      </button>
    </div>
  ),
}));
vi.mock("@/lib/sessionsApi", () => ({
  approve: vi.fn(),
  bindOnlyOnlineRunner: vi.fn(),
  fetchSessionItemsPage: vi.fn(),
  getSession: vi.fn(),
  openSessionStream: vi.fn(),
  postEvent: vi.fn(),
}));
vi.mock("@/lib/sse", () => ({ parseEvent: vi.fn(() => null), parseSseStream: vi.fn() }));

async function* streamEvents(): AsyncIterable<StreamEvent> {
  yield {
    type: "response_created",
    response: { id: "resp_dpia", status: "in_progress", model: "dpia-investigation" },
  };
  yield { type: "text_delta", delta: "Review the proposed evidence change.\n" };
  yield {
    type: "elicitation_request",
    elicitationId: "elicit_dpia",
    message: "Approve this agent action?",
    requestedSchema: {},
    mode: "form",
    phase: "tool_call",
    policyName: "officer-approval",
    contentPreview: "correction proposal",
  };
}

async function* completedStreamEvents(): AsyncIterable<StreamEvent> {
  yield {
    type: "response_created",
    response: { id: "resp_committed", status: "in_progress", model: "dpia-investigation" },
  };
  yield { type: "text_delta", delta: "Streaming answer" };
  yield {
    type: "response_completed",
    response: { id: "resp_committed", status: "completed", model: "dpia-investigation" },
  };
}

beforeEach(() => {
  vi.mocked(fetchSessionItemsPage).mockResolvedValue({ items: [], hasMore: false });
  vi.mocked(getSession).mockResolvedValue({ pendingElicitations: [] } as never);
  vi.mocked(openSessionStream).mockResolvedValue(new Response("stream", { status: 200 }));
  vi.mocked(parseSseStream).mockReturnValue(streamEvents());
  vi.mocked(postEvent).mockResolvedValue({ queued: true });
  vi.mocked(approve).mockResolvedValue({ queued: false });
  vi.mocked(bindOnlyOnlineRunner).mockResolvedValue({
    id: "conv_dpia",
    runnerId: "runner_dpia",
    status: "idle",
  } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DPIA case chat", () => {
  it("posts messages, streams agent text, and resolves inline approvals", async () => {
    const caseData = createStudentSuccessAlertSeed();
    render(
      <DpiaCaseChat
        caseData={caseData}
        sessionId="conv_dpia"
        connecting={false}
        bindingError={null}
        onConnect={vi.fn()}
        onStageProposal={vi.fn()}
        onEditProposal={vi.fn()}
        onApplyProposal={vi.fn()}
        onRejectProposal={vi.fn()}
      />,
    );

    expect(await screen.findByText("Review the proposed evidence change.")).toBeInTheDocument();
    expect(screen.getByText("Approve this agent action?")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("textbox", { name: "Message case agent" }), {
      target: { value: "Draft a correction for the hosting fact." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() =>
      expect(postEvent).toHaveBeenCalledWith("conv_dpia", {
        type: "message",
        data: {
          role: "user",
          content: [
            {
              type: "input_text",
              text: buildDpiaAgentMessage(caseData, "Draft a correction for the hosting fact."),
            },
          ],
        },
      }),
    );
    expect(screen.getByText("Draft a correction for the hosting fact.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() =>
      expect(approve).toHaveBeenCalledWith("conv_dpia", "elicit_dpia", { action: "accept" }),
    );
    expect(screen.getByText("responded")).toBeInTheDocument();
  });

  it("refreshes committed history when a streamed response completes", async () => {
    vi.mocked(parseSseStream).mockReturnValue(completedStreamEvents());
    vi.mocked(fetchSessionItemsPage)
      .mockResolvedValueOnce({ items: [], hasMore: false })
      .mockResolvedValueOnce({
        items: [
          {
            id: "msg_committed",
            response_id: "resp_committed",
            type: "message",
            role: "assistant",
            status: "completed",
            model: "dpia-investigation",
            content: [{ type: "output_text", text: "Committed answer" }],
          },
        ],
        hasMore: false,
      } as never);

    render(
      <DpiaCaseChat
        caseData={createStudentSuccessAlertSeed()}
        sessionId="conv_dpia"
        connecting={false}
        bindingError={null}
        onConnect={vi.fn()}
        onStageProposal={vi.fn()}
        onEditProposal={vi.fn()}
        onApplyProposal={vi.fn()}
        onRejectProposal={vi.fn()}
      />,
    );

    expect(await screen.findByText("Committed answer")).toBeInTheDocument();
    expect(fetchSessionItemsPage).toHaveBeenCalledTimes(2);
  });

  it("shows a retryable failure when the bound session has no online runner", async () => {
    vi.mocked(bindOnlyOnlineRunner).mockResolvedValue(null);
    render(
      <DpiaCaseChat
        caseData={createStudentSuccessAlertSeed()}
        sessionId="conv_dpia"
        connecting={false}
        bindingError={null}
        onConnect={vi.fn()}
        onStageProposal={vi.fn()}
        onEditProposal={vi.fn()}
        onApplyProposal={vi.fn()}
        onRejectProposal={vi.fn()}
      />,
    );

    expect(
      await screen.findByText(/case session is bound, but no runner is online/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry connection" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message case agent" })).toBeDisabled();
  });
});
