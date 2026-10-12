import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { BubbleView } from "@/pages/ChatPage";
import { useChatStore } from "@/store/chatStore";
import { buildBubbles } from "@/lib/renderItems";
import { BlockStream } from "@/lib/blockStream";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { parseEvent } from "@/lib/sse";

afterEach(cleanup);
beforeEach(() => useChatStore.setState({ sessionStatus: "idle" }));

describe("sub-agent activity", () => {
  it.each([
    ["delegated", undefined, "Started"],
    ["returned", undefined, "Completed"],
    ["returned", "failed", "Failed"],
    ["returned", "cancelled", "Stopped"],
  ] as const)("renders %s/%s live and after reload as %s", (phase, status, label) => {
    const item = {
      id: "activity",
      type: "resource_event",
      status: "completed",
      response_id: "activity_turn",
      event_type: `session.subagent.${phase}`,
      resource_type: "session",
      resource_id: "conv_child",
      resource: { title: "Research public positioning", status },
    };
    const event = parseEvent("response.output_item.done", { item });
    expect(event).not.toBeNull();
    const live = buildBubbles(new BlockStream().reduceSync([event!]), null);
    expect(buildBubbles(itemsToBlocks([item]), null)).toEqual(live);
    expect(live).toHaveLength(1);
    render(
      <MemoryRouter
        initialEntries={["/c/parent?file=README.md&view=terminal&message=msg_parent&debug=1"]}
      >
        <BubbleView bubble={live[0]!} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("subagent-activity")).toHaveTextContent(
      `${label} Research public positioning`,
    );
    expect(screen.getByRole("link")).toHaveAttribute("href", "/c/conv_child?debug=1&panel=agents");
    expect(screen.queryByTestId("message-bubble")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy" })).not.toBeInTheDocument();
  });

  it.each([
    { event_type: "session.resource.created", resource_type: "terminal", resource_id: "term_1" },
    { event_type: "session.subagent.delegated", resource_type: "session", resource_id: "" },
  ])("ignores unrelated or malformed resource events: %j", (fields) => {
    const item = {
      id: "resource",
      type: "resource_event",
      response_id: "activity",
      status: "completed",
      ...fields,
    };
    expect(itemsToBlocks([item])).toEqual([]);
    expect(parseEvent("response.output_item.done", { item })).toBeNull();
  });
});
