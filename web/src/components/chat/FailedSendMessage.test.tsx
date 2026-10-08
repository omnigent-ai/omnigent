import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FailedUserMessage } from "@/store/chatStore";

import { FailedSendMessage } from "./FailedSendMessage";

function retained(overrides: Partial<FailedUserMessage> = {}): FailedUserMessage {
  return {
    stableId: "a".repeat(32),
    conversationId: "conv_test",
    agentId: "agent_xyz",
    text: "first message that will fail",
    files: [],
    reason: "Failed to fetch",
    serverRefused: false,
    ...overrides,
  };
}

function renderCard(message: FailedUserMessage) {
  const handlers = {
    onRetry: vi.fn(),
    onCheck: vi.fn(async () => {}),
    onEdit: vi.fn(() => true),
    onDiscard: vi.fn(),
  };
  render(<FailedSendMessage message={message} {...handlers} />);
  return handlers;
}

afterEach(cleanup);

describe("FailedSendMessage", () => {
  it("keeps the text, attachment and reason on the page with a Retry", () => {
    const file = new File(["notes"], "notes.txt", { type: "text/plain" });
    const handlers = renderCard(retained({ files: [file] }));

    const card = screen.getByTestId("failed-send-message");
    expect(card).toHaveAttribute("data-delivery-status", "not_sent");
    expect(card).toHaveTextContent("first message that will fail");
    expect(card).toHaveTextContent("notes.txt");
    expect(card).toHaveTextContent("Failed to send");
    expect(card).toHaveTextContent("Failed to fetch");
    // Attachments are only removable while editing.
    expect(screen.queryByRole("button", { name: "Remove notes.txt" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(handlers.onRetry).toHaveBeenCalledOnce();
  });

  it("offers Check instead of Retry or Edit while delivery is unconfirmed", async () => {
    const handlers = renderCard(retained({ unsettled: true, reason: "" }));

    const card = screen.getByTestId("failed-send-message");
    expect(card).toHaveAttribute("data-delivery-status", "unconfirmed");
    expect(card).toHaveTextContent("Send unconfirmed");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Check" }));
    });
    expect(handlers.onCheck).toHaveBeenCalledOnce();
  });

  it("offers Check again when the check itself rejects", async () => {
    const handlers = renderCard(retained({ unsettled: true, reason: "" }));
    handlers.onCheck.mockRejectedValueOnce(new Error("offline"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Check" }));
    });

    expect(handlers.onCheck).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Check" })).toBeEnabled();
    expect(screen.getByTestId("failed-send-message")).toHaveTextContent("Send unconfirmed");
  });

  it("edits the text and removes an attachment before saving", () => {
    const file = new File(["notes"], "notes.txt", { type: "text/plain" });
    const handlers = renderCard(retained({ files: [file] }));

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const editor = screen.getByLabelText("Edit unsent message");
    expect(editor).toHaveValue("first message that will fail");
    fireEvent.change(editor, { target: { value: "edited message" } });
    fireEvent.click(screen.getByRole("button", { name: "Remove notes.txt" }));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(handlers.onEdit).toHaveBeenCalledWith("edited message", []);
    expect(screen.queryByLabelText("Edit unsent message")).toBeNull();
  });

  it("keeps the editor open when the store refuses the edit", () => {
    const handlers = renderCard(retained());
    handlers.onEdit.mockReturnValue(false);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Edit unsent message"), {
      target: { value: "edit during a retry" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    // A retry owns the message, so the store dropped the edit; the editor
    // stays open with the pending text rather than falsely confirming it.
    expect(handlers.onEdit).toHaveBeenCalledWith("edit during a retry", []);
    expect(screen.getByLabelText("Edit unsent message")).toHaveValue("edit during a retry");
  });

  it("withholds Save, keeping the edit, once delivery turns unconfirmed mid-edit", () => {
    const message = retained();
    const handlers = {
      onRetry: vi.fn(),
      onCheck: vi.fn(async () => {}),
      onEdit: vi.fn(() => true),
      onDiscard: vi.fn(),
    };
    const { rerender } = render(<FailedSendMessage message={message} {...handlers} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Edit unsent message"), {
      target: { value: "edited while the retry settled" },
    });

    // A background retry lost its acknowledgement: the message may already be
    // delivered, so the edit must neither save nor vanish.
    rerender(<FailedSendMessage message={{ ...message, unsettled: true }} {...handlers} />);
    const save = screen.getByRole("button", { name: "Save changes" });
    expect(save).toBeDisabled();
    fireEvent.submit(save.closest("form")!);

    expect(handlers.onEdit).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Edit unsent message")).toHaveValue(
      "edited while the retry settled",
    );
    expect(screen.getByTestId("failed-send-message")).toHaveTextContent("Send unconfirmed");
  });

  it("cancels an edit with Escape without saving", () => {
    const handlers = renderCard(retained());

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const editor = screen.getByLabelText("Edit unsent message");
    fireEvent.change(editor, { target: { value: "abandoned edit" } });
    fireEvent.keyDown(editor, { key: "Escape" });

    expect(screen.queryByLabelText("Edit unsent message")).toBeNull();
    expect(screen.getByTestId("failed-send-message")).toHaveTextContent(
      "first message that will fail",
    );
    expect(handlers.onEdit).not.toHaveBeenCalled();
  });

  it("refuses to save a message with neither text nor attachments", () => {
    const handlers = renderCard(retained());

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Edit unsent message"), { target: { value: "   " } });
    const save = screen.getByRole("button", { name: "Save changes" });
    expect(save).toBeDisabled();
    fireEvent.click(save);

    expect(handlers.onEdit).not.toHaveBeenCalled();
  });

  it("discards the message", () => {
    const handlers = renderCard(retained());

    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(handlers.onDiscard).toHaveBeenCalledOnce();
  });
});
