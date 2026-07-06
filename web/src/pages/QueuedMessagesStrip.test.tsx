// Tests for QueuedMessagesStrip — the presentational strip above the composer
// listing messages queued while the agent is busy. It's a pure prop-driven
// component (no store access), so we exercise it with plain props.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

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
    const { container } = render(<QueuedMessagesStrip messages={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders one row per queued message, in order", () => {
    render(<QueuedMessagesStrip messages={[msg("q_1", "first"), msg("q_2", "second")]} />);
    expect(screen.getByText("first")).toBeInTheDocument();
    expect(screen.getByText("second")).toBeInTheDocument();
    // Each row carries the "Queued" label.
    expect(screen.getAllByText("Queued")).toHaveLength(2);
  });
});
