// Tests for QueuedMessagesStrip — the presentational strip above the composer
// listing messages queued while the agent is busy. It's a pure prop-driven
// component (no store access), so we exercise it with plain props.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { QueuedMessage } from "@/store/chatStore";
import { QueuedMessagesStrip } from "./QueuedMessagesStrip";

const msg = (queueId: string, text: string): QueuedMessage => ({
  queueId,
  text,
  conversationId: "conv_abc",
});

afterEach(cleanup);

describe("QueuedMessagesStrip", () => {
  it("renders nothing when the queue is empty", () => {
    const { container } = render(
      <QueuedMessagesStrip messages={[]} onDelete={vi.fn()} onEdit={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders one row per queued message, in order", () => {
    render(
      <QueuedMessagesStrip
        messages={[msg("q_1", "first"), msg("q_2", "second")]}
        onDelete={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    expect(screen.getByText("first")).toBeInTheDocument();
    expect(screen.getByText("second")).toBeInTheDocument();
  });

  it.each([
    ["screenshot.png", "image/png"],
    ["report.pdf", "application/pdf"],
    ["notes.txt", "text/plain"],
  ])("shows the filename for an attachment-only message: %s", (name, type) => {
    render(
      <QueuedMessagesStrip
        messages={[{ ...msg("q_1", ""), files: [new File([], name, { type })] }]}
        onDelete={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    expect(screen.getByText(name)).toBeInTheDocument();
  });

  it.each(["", "Look at these"])(
    "shows one filename and an overflow count with all filenames on hover (text: %j)",
    (text) => {
      const filenames = ["Screenshot before the layout change.png", "after.png", "notes.txt"];
      render(
        <QueuedMessagesStrip
          messages={[{ ...msg("q_1", text), files: filenames.map((name) => new File([], name)) }]}
          onDelete={vi.fn()}
          onEdit={vi.fn()}
        />,
      );
      const chip = screen.getByTestId("queued-message-attachments");
      expect(screen.getByText(filenames[0]!)).toHaveClass("truncate");
      expect(screen.getByText("+2")).toHaveAttribute("aria-hidden", "true");
      expect(chip).toHaveAttribute("title", filenames.join("\n"));
      expect(chip.querySelector(".sr-only")).toHaveTextContent("after.png, notes.txt");
    },
  );

  it("shows filenames when the message text is only whitespace", () => {
    render(
      <QueuedMessagesStrip
        messages={[{ ...msg("q_1", " \n\t "), files: [new File([], "screenshot.png")] }]}
        onDelete={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    expect(screen.getByText("screenshot.png")).toBeInTheDocument();
  });

  it.each(["", "Look at this"])(
    "uses the composer/upload filename for an unnamed attachment (text: %j)",
    (text) => {
      render(
        <QueuedMessagesStrip
          messages={[{ ...msg("q_1", text), files: [new File([], "")] }]}
          onDelete={vi.fn()}
          onEdit={vi.fn()}
        />,
      );
      expect(screen.getByText("image.png")).toBeInTheDocument();
      expect(screen.getByTestId("queued-message-attachments")).toHaveAttribute(
        "title",
        "image.png",
      );
    },
  );

  it.each([
    ["short", "Look at this"],
    ["long", "Explain the layout in this screenshot. ".repeat(30).trim()],
  ])("keeps separate truncating previews for %s text and attachments", (_length, text) => {
    render(
      <QueuedMessagesStrip
        messages={[{ ...msg("q_1", text), files: [new File([], "screenshot.png")] }]}
        onDelete={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    const textPreview = screen.getByText(text);
    const attachmentPreview = screen.getByText("screenshot.png");
    expect(textPreview).toHaveClass("truncate");
    expect(textPreview).toHaveAttribute("title", text);
    expect(attachmentPreview).toHaveClass("truncate");
    const chip = screen.getByTestId("queued-message-attachments");
    expect(chip).toHaveAttribute("title", "screenshot.png");
    expect(chip).toHaveClass("shrink-0");
    expect(chip.parentElement).toBe(textPreview.parentElement);
  });

  it("does not render an attachment chip for a text-only message", () => {
    render(
      <QueuedMessagesStrip
        messages={[{ ...msg("q_1", "Just text"), files: [] }]}
        onDelete={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    expect(screen.getByText("Just text")).toBeInTheDocument();
    expect(screen.queryByTestId("queued-message-attachments")).not.toBeInTheDocument();
  });

  it("keeps the filename visible when text is added to an attachment-only queue entry", () => {
    const file = new File([], "screenshot.png", { type: "image/png" });
    const props = { onDelete: vi.fn(), onEdit: vi.fn() };
    const { rerender } = render(
      <QueuedMessagesStrip {...props} messages={[{ ...msg("q_1", ""), files: [file] }]} />,
    );
    expect(screen.getByText(file.name)).toBeInTheDocument();
    rerender(
      <QueuedMessagesStrip {...props} messages={[{ ...msg("q_1", "Hey"), files: [file] }]} />,
    );
    expect(screen.getByText("Hey")).toBeInTheDocument();
    expect(screen.getByText(file.name)).toBeInTheDocument();
    expect(screen.queryByText("+0")).not.toBeInTheDocument();
  });

  it("calls onDelete with the row's queueId when its remove button is clicked", () => {
    const onDelete = vi.fn();
    render(
      <QueuedMessagesStrip
        messages={[msg("q_1", "first"), msg("q_2", "second")]}
        onDelete={onDelete}
        onEdit={vi.fn()}
      />,
    );
    const buttons = screen.getAllByRole("button", { name: "Remove queued message" });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[1]!);
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(onDelete).toHaveBeenCalledWith("q_2");
  });

  it("calls onEdit with the row's queueId when its edit button is clicked", () => {
    const onEdit = vi.fn();
    render(
      <QueuedMessagesStrip
        messages={[msg("q_1", "first"), msg("q_2", "second")]}
        onDelete={vi.fn()}
        onEdit={onEdit}
      />,
    );
    const buttons = screen.getAllByRole("button", { name: "Edit queued message" });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[0]!);
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(onEdit).toHaveBeenCalledWith("q_1");
  });

  it("shows no steer button when onSteer is omitted", () => {
    render(
      <QueuedMessagesStrip messages={[msg("q_1", "first")]} onDelete={vi.fn()} onEdit={vi.fn()} />,
    );
    expect(screen.queryByRole("button", { name: "Send queued message now" })).toBeNull();
  });

  it("calls onSteer with the row's queueId when its steer button is clicked", () => {
    const onSteer = vi.fn();
    render(
      <TooltipProvider>
        <QueuedMessagesStrip
          messages={[msg("q_1", "first"), msg("q_2", "second")]}
          onDelete={vi.fn()}
          onEdit={vi.fn()}
          onSteer={onSteer}
        />
      </TooltipProvider>,
    );
    const buttons = screen.getAllByRole("button", { name: "Send queued message now" });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[1]!);
    expect(onSteer).toHaveBeenCalledTimes(1);
    expect(onSteer).toHaveBeenCalledWith("q_2");
  });

  it("marks failed messages and offers an explicit retry", () => {
    const onSteer = vi.fn();
    render(
      <TooltipProvider>
        <QueuedMessagesStrip
          messages={[{ ...msg("q_failed", "Keep this message"), requiresRetry: true }]}
          onDelete={vi.fn()}
          onEdit={vi.fn()}
          onSteer={onSteer}
        />
      </TooltipProvider>,
    );
    expect(screen.getByText("Send failed")).toBeInTheDocument();
    expect(screen.getByText("Keep this message")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry queued message" }));
    expect(onSteer).toHaveBeenCalledWith("q_failed");
  });

  it("gives every row action a 44px mobile tap target with a composer-sized icon", () => {
    render(
      <TooltipProvider>
        <QueuedMessagesStrip
          messages={[msg("q_1", "first")]}
          onDelete={vi.fn()}
          onEdit={vi.fn()}
          onSteer={vi.fn()}
          onReorder={vi.fn()}
        />
      </TooltipProvider>,
    );
    for (const name of [
      "Reorder queued message",
      "Send queued message now",
      "Edit queued message",
      "Remove queued message",
    ]) {
      const button = screen.getByRole("button", { name });
      // Keep the 44px touch target while matching the composer's 16px glyphs.
      expect(button, name).toHaveClass("max-md:size-11");
      expect(button.querySelector("svg"), name).toHaveClass("max-md:size-4");
    }
  });

  it("shows a drag handle per row only when onReorder is provided", () => {
    const { rerender } = render(
      <QueuedMessagesStrip messages={[msg("q_1", "first")]} onDelete={vi.fn()} onEdit={vi.fn()} />,
    );
    // No reorder handler → no grip (the row shows the clock icon instead).
    expect(screen.queryByRole("button", { name: "Reorder queued message" })).toBeNull();

    rerender(
      <QueuedMessagesStrip
        messages={[msg("q_1", "first"), msg("q_2", "second")]}
        onDelete={vi.fn()}
        onEdit={vi.fn()}
        onReorder={vi.fn()}
      />,
    );
    expect(screen.getAllByRole("button", { name: "Reorder queued message" })).toHaveLength(2);
  });
});
