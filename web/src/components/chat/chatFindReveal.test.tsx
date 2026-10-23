import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConversationItem } from "@/lib/conversationItems";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { type Bubble, buildBubbles } from "@/lib/renderItems";
import type { MessageJumpTarget } from "@/hooks/useMessageDeepLink";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useChatStore } from "@/store/chatStore";
import { useChatFindHighlight } from "./ChatFindBar";
import { ReasoningView } from "@/components/blocks/ReasoningView";
import { SlashCommandCard } from "@/components/blocks/SlashCommandCard";
import { SmartRoutingCard } from "@/components/blocks/SmartRoutingCard";
import { SystemMessageView } from "@/components/blocks/SystemMessage";
import { TerminalCommandCard } from "@/components/blocks/TerminalCommandCard";
import { BubbleView, bubbleKey } from "./chatBubbleParts";
import {
  type ChatFindReveal,
  ChatFindRevealContext,
  findRevealsFor,
  useChatFindReveal,
} from "./chatFindReveal";

const turn = { response_id: "resp_1", status: "completed" };
// The word sits only in the tool's output and in the reasoning, never in visible text.
const toolTurn: ConversationItem[] = [
  { ...turn, id: "msg_user", type: "message", role: "user", content: [prompt("ship it")] },
  { ...turn, id: "rs_1", type: "reasoning", summary: [{ type: "summary_text", text: "plan" }] },
  {
    ...turn,
    id: "fc_1",
    type: "function_call",
    name: "sys_os_shell",
    arguments: "{}",
    call_id: "c1",
  },
  { ...turn, id: "fco_1", type: "function_call_output", call_id: "c1", output: "ran zorblat" },
  { ...turn, id: "msg_reply", type: "message", role: "assistant", content: [text("Done.")] },
] as ConversationItem[];

function text(value: string) {
  return { type: "output_text" as const, text: value };
}

function prompt(value: string) {
  return { type: "input_text" as const, text: value };
}

function bubblesOf(items: ConversationItem[]): Bubble[] {
  return buildBubbles(itemsToBlocks(items), null);
}

/** Bubbles under the find painter, with `reveal` as the find bar's current reveal. */
function Transcript({ bubbles, reveal }: { bubbles: Bubble[]; reveal: ChatFindReveal | null }) {
  const [root, setRoot] = useState<HTMLElement | null>(null);
  useChatFindHighlight(reveal?.query ?? "", root);
  return (
    <TooltipProvider>
      <ChatFindRevealContext.Provider value={reveal}>
        <div ref={setRoot}>
          {bubbles.map((bubble) => (
            <BubbleView key={bubbleKey(bubble)} bubble={bubble} />
          ))}
        </div>
      </ChatFindRevealContext.Provider>
    </TooltipProvider>
  );
}

const highlights = new Map<string, { ranges: Range[] }>();

function paintedRanges(): Range[] {
  return highlights.get("chat-find")?.ranges ?? [];
}

beforeEach(() => {
  highlights.clear();
  vi.stubGlobal("CSS", { escape: (value: string) => value, highlights });
  vi.stubGlobal(
    "Highlight",
    class {
      ranges: Range[];
      constructor(...ranges: Range[]) {
        this.ranges = ranges;
      }
    },
  );
  useChatStore.setState({ sessionStatus: "idle", status: "idle", blocks: [] });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("find in chat reveals collapsed content", () => {
  it("opens the Worked fold, tool group and tool call that hold a tool-output match, and paints it", () => {
    const bubbles = bubblesOf(toolTurn);
    const reveal = { anchor: "resp_1", itemIds: ["fco_1"], query: "zorblat" };
    const view = render(<Transcript bubbles={bubbles} reveal={null} />);
    const fold = screen.getByTestId("turn-worked-fold");
    expect(fold.getAttribute("data-state")).toBe("closed");

    view.rerender(<Transcript bubbles={bubbles} reveal={reveal} />);
    expect(fold.getAttribute("data-state")).toBe("open");
    expect(fold.querySelector("[data-chat-find-current]")?.textContent).toContain("ran zorblat");
    expect(paintedRanges().length).toBeGreaterThan(0);
    for (const range of paintedRanges()) expect(fold.contains(range.startContainer)).toBe(true);

    // Closing the bar closes everything find opened.
    view.rerender(<Transcript bubbles={bubbles} reveal={null} />);
    expect(fold.getAttribute("data-state")).toBe("closed");
    expect(fold.querySelectorAll("[data-state=open]")).toHaveLength(0);
  });

  it("opens only the reasoning section for a reasoning match", () => {
    const items = toolTurn.map((item) =>
      item.id === "rs_1"
        ? { ...item, summary: [{ type: "summary_text", text: "think zorblat" }] }
        : item,
    ) as ConversationItem[];
    const reveal = { anchor: "resp_1", itemIds: ["rs_1"], query: "zorblat" };
    render(<Transcript bubbles={bubblesOf(items)} reveal={reveal} />);
    const fold = screen.getByTestId("turn-worked-fold");
    expect(fold.getAttribute("data-state")).toBe("open");
    expect(screen.queryByText("ran zorblat")).toBeNull();
    expect(paintedRanges().map((range) => range.startContainer.textContent)).toEqual([
      "think zorblat",
    ]);
  });

  it("opens every fold of the bubble when no rendered item holds the match", () => {
    const reveal = { anchor: "resp_1", itemIds: ["item_unknown"], query: "ran" };
    render(<Transcript bubbles={bubblesOf(toolTurn)} reveal={reveal} />);
    expect(screen.getByTestId("turn-worked-fold").getAttribute("data-state")).toBe("open");
  });

  it("expands a collapsed long user prompt that holds the match, and collapses it again", async () => {
    const long = `${"filler ".repeat(2000)}zorblat at the end`;
    const items = [
      { ...turn, id: "msg_long", type: "message", role: "user", content: [prompt(long)] },
    ] as ConversationItem[];
    const bubbles = bubblesOf(items);
    const view = render(<Transcript bubbles={bubbles} reveal={null} />);
    expect(screen.getByText(/Show full prompt/)).toBeTruthy();

    const reveal = { anchor: "msg_long", itemIds: ["msg_long"], query: "zorblat" };
    view.rerender(<Transcript bubbles={bubbles} reveal={reveal} />);
    expect(screen.getByText("Collapse prompt")).toBeTruthy();
    await waitFor(() => expect(paintedRanges()).toHaveLength(1));

    view.rerender(<Transcript bubbles={bubbles} reveal={{ ...reveal, anchor: "resp_other" }} />);
    expect(screen.getByText(/Show full prompt/)).toBeTruthy();
    await waitFor(() => expect(paintedRanges()).toHaveLength(0));
  });

  it("opens an error's message and the diagnostic tab that hold the match", () => {
    const errorTurn = (message: string) =>
      bubblesOf([
        {
          ...turn,
          id: "err_1",
          type: "error",
          source: "execution",
          code: "workspace_missing",
          message,
        },
      ] as ConversationItem[]);
    const reveal = { anchor: "resp_1", itemIds: ["err_1"], query: "zorblat" };
    const view = render(
      <Transcript
        bubbles={errorTurn("workspace path does not exist: /home/demo/zorblat")}
        reveal={reveal}
      />,
    );
    expect(screen.getByTestId("error-message-content").textContent).toContain("zorblat");
    view.unmount();

    const diagnostics =
      "it failed\nTerminal diagnostics:\nboot ok\nLast captured output:\nzorblat crash";
    const second = render(<Transcript bubbles={errorTurn(diagnostics)} reveal={null} />);
    expect(screen.queryByTestId("error-message-content")).toBeNull();
    second.rerender(<Transcript bubbles={errorTurn(diagnostics)} reveal={reveal} />);
    expect(screen.getByTestId("error-diagnostics-content").textContent).toBe("zorblat crash");
    second.rerender(<Transcript bubbles={errorTurn(diagnostics)} reveal={null} />);
    expect(screen.queryByTestId("error-message-content")).toBeNull();
  });

  it("keeps a container the reader closed while find revealed it closed, until find reveals it again", () => {
    const bubbles = bubblesOf(toolTurn);
    const reveal = { anchor: "resp_1", itemIds: ["fco_1"], query: "zorblat" };
    const view = render(<Transcript bubbles={bubbles} reveal={reveal} />);
    const fold = screen.getByTestId("turn-worked-fold");
    act(() => {
      fireEvent.click(screen.getByText("Worked"));
    });
    expect(fold.getAttribute("data-state")).toBe("closed");

    view.rerender(<Transcript bubbles={bubbles} reveal={null} />);
    view.rerender(<Transcript bubbles={bubbles} reveal={{ ...reveal }} />);
    expect(fold.getAttribute("data-state")).toBe("open");
  });
});

describe("find in chat reveals each collapsible card", () => {
  const cards = {
    "slash command": (reveal?: "current") => (
      <SlashCommandCard
        kind="command"
        name="effort"
        arguments=""
        output="zorblat out"
        findReveal={reveal}
      />
    ),
    "terminal output": (reveal?: "current") => (
      <TerminalCommandCard
        kind="output"
        input={null}
        stdout="zorblat out"
        stderr={null}
        findReveal={reveal}
      />
    ),
    "smart routing": (reveal?: "current") => (
      <SmartRoutingCard
        arguments={{}}
        output={JSON.stringify({ recommendations: [], note: "zorblat out" })}
        state="output-available"
        findReveal={reveal}
      />
    ),
    "system message": (reveal?: "current") => (
      <SystemMessageView
        message={{ kind: "generic", label: "note", body: "zorblat out" }}
        revealed={!!reveal}
      />
    ),
  };

  it.each(Object.entries(cards))("opens the %s that holds the match", (_name, card) => {
    const view = render(<TooltipProvider>{card()}</TooltipProvider>);
    expect(view.container.textContent).not.toContain("zorblat out");
    view.rerender(<TooltipProvider>{card("current")}</TooltipProvider>);
    expect(view.container.textContent).toContain("zorblat out");
  });

  it.each(Object.entries(cards))(
    "keeps the %s the reader opened before find open after find",
    (_name, card) => {
      const view = render(<TooltipProvider>{card()}</TooltipProvider>);
      fireEvent.click(screen.getAllByRole("button")[0]!);
      expect(view.container.textContent).toContain("zorblat out");
      view.rerender(<TooltipProvider>{card("current")}</TooltipProvider>);
      view.rerender(<TooltipProvider>{card()}</TooltipProvider>);
      expect(view.container.textContent).toContain("zorblat out");
    },
  );

  it("keeps reasoning the reader opened before find open, and reveals it once closed", () => {
    const reasoning = (reveal?: "current") => (
      <TooltipProvider>
        <ReasoningView
          text="zorblat thought"
          isStreaming={false}
          duration={2}
          findReveal={reveal}
        />
      </TooltipProvider>
    );
    const view = render(reasoning());
    fireEvent.click(screen.getByRole("button"));
    view.rerender(reasoning("current"));
    view.rerender(reasoning());
    expect(screen.getByText("zorblat thought")).toBeTruthy();

    fireEvent.click(screen.getByRole("button"));
    expect(screen.queryByText("zorblat thought")).toBeNull();
    view.rerender(reasoning("current"));
    expect(screen.getByText("zorblat thought")).toBeTruthy();
  });

  it("expands a tool output whose match lies past the preview", () => {
    const longOutput = `${"line\n".repeat(100)}zorblat`;
    const items = toolTurn.map((item) =>
      item.id === "fco_1" ? { ...item, output: longOutput } : item,
    ) as ConversationItem[];
    const reveal = { anchor: "resp_1", itemIds: ["fco_1"], query: "zorblat" };
    render(<Transcript bubbles={bubblesOf(items)} reveal={reveal} />);
    expect(screen.getByText(/Showing full output/)).toBeTruthy();
  });
});

describe("findRevealsFor", () => {
  it("finds a tool call by its output item and reasoning by its item, the last id current", () => {
    const [, assistant] = bubblesOf(toolTurn);
    if (assistant?.kind !== "assistant") throw new Error("expected an assistant bubble");
    const reveals = findRevealsFor(assistant.items, ["rs_1", "fco_1"]);
    expect([...reveals].map(([item, reveal]) => [item.kind, reveal])).toEqual([
      ["reasoning", "kept"],
      ["tool", "current"],
    ]);
  });
});

describe("useChatFindReveal", () => {
  const anchors: Record<string, string> = { a1: "resp_a", a2: "resp_a", b1: "resp_b" };
  const resolveAnchor = (id: string) => anchors[id] ?? null;
  const step = (id: string, n: number): MessageJumpTarget => ({
    key: `find:${n}:${id}`,
    candidates: [id],
    query: "zorblat",
  });

  it("keeps matches stepped to in one bubble and starts over in another", () => {
    const { result, rerender } = renderHook(
      ({ target }: { target: MessageJumpTarget | null }) =>
        useChatFindReveal(target, resolveAnchor),
      { initialProps: { target: step("a1", 1) as MessageJumpTarget | null } },
    );
    expect(result.current).toEqual({ anchor: "resp_a", itemIds: ["a1"], query: "zorblat" });
    rerender({ target: step("a2", 2) });
    expect(result.current?.itemIds).toEqual(["a1", "a2"]);
    rerender({ target: step("b1", 3) });
    expect(result.current).toEqual({ anchor: "resp_b", itemIds: ["b1"], query: "zorblat" });
    rerender({ target: null });
    expect(result.current).toBeNull();
  });
});
