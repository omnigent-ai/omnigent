import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FailedUserMessage } from "@/store/chatStore";
import { FailedSendMessage } from "./FailedSendMessage";

afterEach(cleanup);

const submission: FailedUserMessage = {
  stableId: "a".repeat(32),
  conversationId: "conversation-1",
  agentId: "agent-1",
  text: "Please summarize the attached notes.",
  files: [new File(["notes"], "notes.txt", { type: "text/plain" })],
  content: [{ type: "input_text", text: "Please summarize the attached notes." }],
  createdAtS: 1,
  status: "not_sent",
};

describe("failed message recovery", () => {
  it("keeps text and attachments offline, and enables an explicit retry after reconnect", () => {
    const onRetry = vi.fn();
    const props = { message: submission, onRetry, onCheck: vi.fn(), onEdit: vi.fn() };
    const { rerender } = render(<FailedSendMessage {...props} online={false} />);

    expect(screen.getByText(submission.text)).toBeInTheDocument();
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Failed to send");
    expect(screen.getByRole("status")).toHaveTextContent("Offline");

    rerender(<FailedSendMessage {...props} online />);
    expect(screen.getByRole("status")).toHaveTextContent("Failed to send");
    expect(screen.queryByText("Offline")).not.toBeInTheDocument();
    expect(onRetry).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("edits the saved message independently of a newer composer draft", () => {
    const onEdit = vi.fn();
    const onRetry = vi.fn();
    const rejectedFile = new File(["rejected"], "rejected.exe");
    render(
      <>
        <FailedSendMessage
          message={{ ...submission, files: [...submission.files, rejectedFile] }}
          online={false}
          onRetry={onRetry}
          onCheck={vi.fn()}
          onEdit={onEdit}
        />
        <textarea aria-label="Composer" defaultValue="A newer draft" />
      </>,
    );
    expect(screen.queryByRole("button", { name: "Remove rejected.exe" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("textbox", { name: "Edit unsent message" })).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove rejected.exe" }));
    expect(screen.queryByText("rejected.exe")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Edit unsent message" })).toHaveFocus();
    fireEvent.change(screen.getByRole("textbox", { name: "Edit unsent message" }), {
      target: { value: "Please summarize just the open questions." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(onEdit).toHaveBeenCalledWith(
      "Please summarize just the open questions.",
      submission.files,
    );
    expect(onEdit.mock.calls[0]![1][0]).toBe(submission.files[0]);
    expect(screen.getByRole("textbox", { name: "Composer" })).toHaveValue("A newer draft");
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toHaveFocus();
    expect(onRetry).not.toHaveBeenCalled();
  });

  it("restores removed attachments when editing is canceled", () => {
    const onEdit = vi.fn();
    render(
      <FailedSendMessage
        message={submission}
        online
        onRetry={vi.fn()}
        onCheck={vi.fn()}
        onEdit={onEdit}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove notes.txt" }));
    expect(screen.queryByText("notes.txt")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onEdit).not.toHaveBeenCalled();
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Remove notes.txt" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("button", { name: "Remove notes.txt" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onEdit).toHaveBeenCalledWith(submission.text, submission.files);
  });

  it("allows saving without attachments only when message text remains", () => {
    const onEdit = vi.fn();
    render(
      <FailedSendMessage
        message={{ ...submission, text: "" }}
        online
        onRetry={vi.fn()}
        onCheck={vi.fn()}
        onEdit={onEdit}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove notes.txt" }));
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    const editor = screen.getByRole("textbox", { name: "Edit unsent message" });
    fireEvent.submit(editor.closest("form")!);
    expect(onEdit).not.toHaveBeenCalled();

    fireEvent.change(editor, { target: { value: "Please continue without the attachment." } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onEdit).toHaveBeenCalledWith("Please continue without the attachment.", []);
  });

  it("offers a delivery check without a resend when the request may have been accepted", () => {
    const onCheck = vi.fn();
    const onRetry = vi.fn();
    const props = {
      message: { ...submission, status: "unknown" as const },
      onCheck,
      onRetry,
      onEdit: vi.fn(),
    };
    const { rerender } = render(<FailedSendMessage {...props} online />);

    expect(screen.getByRole("status")).toHaveTextContent("Send unconfirmed");
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(onCheck).toHaveBeenCalledTimes(1);
    expect(onRetry).not.toHaveBeenCalled();

    rerender(
      <FailedSendMessage {...props} message={{ ...submission, status: "checking" }} online />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Checking send status…");
    expect(screen.queryByRole("button", { name: "Check" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("stops editing if the message becomes uncertain, without saving or retrying", () => {
    const onEdit = vi.fn();
    const onRetry = vi.fn();
    const props = { message: submission, onEdit, onRetry, onCheck: vi.fn() };
    const { rerender } = render(<FailedSendMessage {...props} online />);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Edit unsent message" }), {
      target: { value: "A revised request" },
    });
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();

    rerender(
      <FailedSendMessage {...props} message={{ ...submission, status: "unknown" }} online />,
    );

    expect(screen.queryByRole("textbox", { name: "Edit unsent message" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check" })).toBeEnabled();
    expect(screen.getByText(submission.text)).toBeInTheDocument();
    expect(onEdit).not.toHaveBeenCalled();
    expect(onRetry).not.toHaveBeenCalled();
  });
});
