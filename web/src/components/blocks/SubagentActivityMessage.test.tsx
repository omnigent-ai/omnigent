import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
import { writeOpenSubagentsInPanel } from "@/lib/subagentPanelPreferences";
import { SubagentPanelOpenerContext } from "@/shell/subagentPanelOpener";
import { BubbleView } from "@/pages/ChatPage";
import { useChatStore } from "@/store/chatStore";
import { buildBubbles } from "@/lib/renderItems";
import type { Bubble } from "@/lib/renderItems";
import { BlockStream } from "@/lib/blockStream";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { parseEvent } from "@/lib/sse";

afterEach(() => {
  cleanup();
  localStorage.clear();
});
beforeEach(() => useChatStore.setState({ sessionStatus: "idle" }));

describe("sub-agent activity", () => {
  it("places a trailing notice after its assistant actions", () => {
    const bubble: Extract<Bubble, { kind: "assistant" }> = {
      kind: "assistant",
      responseId: "activity_turn",
      stableId: "reply",
      lifecycle: "completed",
      error: null,
      items: [
        { kind: "text", itemId: "reply", text: "I started the review.", final: true },
        {
          kind: "subagent_activity",
          itemId: "activity",
          data: {
            type: "resource_event",
            event_type: "session.subagent.delegated",
            resource_type: "session",
            resource_id: "conv_child",
            resource: { title: "Parfit" },
          },
        },
      ],
    };
    render(
      <MemoryRouter>
        <BubbleView bubble={bubble} />
      </MemoryRouter>,
    );

    const children = screen.getByTestId("message-bubble").children;
    expect(children).toHaveLength(3);
    expect(children[0]).toContainElement(screen.getByTestId("assistant-text-section"));
    expect(children[1]).toContainElement(screen.getByRole("button", { name: "Copy" }));
    expect(children[2]).toContainElement(screen.getByTestId("subagent-activity"));
    expect(screen.getByRole("button", { name: "Copy" })).toHaveClass(
      "[@media(pointer:coarse)]:size-8",
    );
  });

  it.each([
    ["delegated", undefined, "started working"],
    ["returned", undefined, "finished working"],
    ["returned", "failed", "failed"],
    ["returned", "cancelled", "stopped"],
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
    const activity = screen.getByTestId("subagent-activity");
    expect(activity).toHaveTextContent(`Research public positioning ${label}`);
    expect(activity.querySelector(".border-dashed")).toBeNull();
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

  it.each([
    [true, "/c/parent"],
    [false, "/c/conv_child"],
  ])("with the panel setting %s, a click lands on %s", (enabled, pathname) => {
    writeOpenSubagentsInPanel(enabled);
    const opener = vi.fn(() => true);
    const item = {
      id: "activity",
      type: "resource_event",
      status: "completed",
      response_id: "activity_turn",
      event_type: "session.subagent.delegated",
      resource_type: "session",
      resource_id: "conv_child",
      resource: { title: "Research public positioning" },
    };
    const [bubble] = buildBubbles(itemsToBlocks([item]), null);
    function CurrentPath() {
      return <output data-testid="current-path">{useLocation().pathname}</output>;
    }
    render(
      <MemoryRouter initialEntries={["/c/parent"]}>
        <SubagentPanelOpenerContext.Provider value={opener}>
          <BubbleView bubble={bubble!} />
        </SubagentPanelOpenerContext.Provider>
        <CurrentPath />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("link"));

    expect(screen.getByTestId("current-path")).toHaveTextContent(pathname);
    if (enabled) expect(opener).toHaveBeenCalledWith("conv_child");
    else expect(opener).not.toHaveBeenCalled();
  });
});
