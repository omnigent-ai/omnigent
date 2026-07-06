// Tests for QueuedMessagesStrip — the presentational strip above the composer
// listing messages queued while the agent is busy. It's a pure prop-driven
// component (no store access), so we exercise it with plain props.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
    // Each row carries the "Queued" label.
    expect(screen.getAllByText("Queued")).toHaveLength(2);
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
});
