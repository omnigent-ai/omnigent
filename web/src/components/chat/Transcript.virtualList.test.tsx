import { measureElement, useVirtualizer } from "@tanstack/react-virtual";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import type { Bubble } from "@/lib/renderItems";
import { Conversation, ConversationContent } from "@/components/ai-elements/conversation";
import { useChatStore } from "@/store/chatStore";
import {
  isNativeFindShortcut,
  type TranscriptGeometry,
  VirtualBubbleList,
  visibleTranscriptBubbles,
} from "./Transcript";

afterEach(cleanup);

const bubble: Extract<Bubble, { kind: "user" }> = {
  kind: "user",
  itemId: "user-1",
  content: [{ type: "input_text", text: "hello" }],
};

const assistantBubble: Extract<Bubble, { kind: "assistant" }> = {
  kind: "assistant",
  responseId: "response-1",
  stableId: "response-1",
  lifecycle: "completed",
  error: null,
  items: [{ kind: "text", itemId: "assistant-1", text: "hello back", final: true }],
};

it("omits hidden sub-agent wakes from visible row geometry", () => {
  const wake: Bubble = {
    kind: "user",
    itemId: "wake",
    content: [
      {
        type: "input_text",
        text: "[System: sub-agent Dewey finished (completed) \u2014 1 result waiting in inbox. Call sys_read_inbox to collect.]",
      },
    ],
  };
  const timer: Bubble = {
    kind: "user",
    itemId: "timer",
    content: [{ type: "input_text", text: "[System: timer reminder fired]" }],
  };
  const bubbles = [assistantBubble, wake, timer, bubble];
  const visible = visibleTranscriptBubbles(bubbles);

  expect(visible).toEqual([assistantBubble, timer, bubble]);
  const malformed: Bubble = {
    kind: "user",
    itemId: "malformed",
    content: [{ type: "input_text", text: null as unknown as string }],
  };
  expect(visibleTranscriptBubbles([malformed, wake])).toEqual([malformed]);
  const wakeWithBody: Bubble = {
    ...wake,
    itemId: "wake-with-body",
    content: [...wake.content, { type: "input_text", text: "\nOther context" }],
  };
  expect(visibleTranscriptBubbles([wakeWithBody])).toEqual([wakeWithBody]);
  expect(visibleTranscriptBubbles(visible)).toBe(visible);
  render(list(false, document.createElement("div"), vi.fn(), false, visible));
  expect(vi.mocked(useVirtualizer).mock.lastCall?.[0].count).toBe(3);
});

it("drops messages from other sessions only when they are hidden", () => {
  const peer: Bubble = {
    kind: "user",
    itemId: "peer",
    content: [{ type: "input_text", text: "[Message from session s_src]\nreport" }],
  };
  const bubbles = [bubble, peer, assistantBubble];
  expect(visibleTranscriptBubbles(bubbles)).toBe(bubbles);
  expect(visibleTranscriptBubbles(bubbles, true)).toEqual([bubble, assistantBubble]);
});

function list(
  hasTasks: boolean,
  scrollEl: HTMLElement,
  onGeometryChange: (geometry: TranscriptGeometry) => void = vi.fn(),
  disableVirtualization = false,
  bubbles: Bubble[] = [bubble],
) {
  return (
    <Conversation>
      <ConversationContent>
        <VirtualBubbleList
          bubbles={bubbles}
          scrollEl={scrollEl}
          lastAssistantIndex={-1}
          showsWorking={false}
          sessionIdle
          conversationId={undefined}
          hasTasks={hasTasks}
          disableVirtualization={disableVirtualization}
          onGeometryChange={onGeometryChange}
        />
      </ConversationContent>
    </Conversation>
  );
}

function messageList(bubbles: Bubble[], showsWorking: boolean, sessionIdle: boolean) {
  return (
    <Conversation>
      <ConversationContent>
        <VirtualBubbleList
          bubbles={bubbles}
          scrollEl={null}
          lastAssistantIndex={bubbles.findLastIndex((item) => item.kind === "assistant")}
          showsWorking={showsWorking}
          sessionIdle={sessionIdle}
          conversationId="conv-1"
          hasTasks={false}
          disableVirtualization
          onGeometryChange={vi.fn()}
        />
      </ConversationContent>
    </Conversation>
  );
}

function actionFooter(button: HTMLElement): HTMLElement {
  return button.parentElement!.parentElement!;
}

it("remeasures scrollMargin when task padding changes without changing bubbles", async () => {
  const scrollEl = document.createElement("div");
  Object.defineProperties(scrollEl, {
    scrollTop: { configurable: true, writable: true, value: 0 },
    clientHeight: { configurable: true, value: 500 },
    scrollHeight: { configurable: true, value: 1_000 },
  });
  scrollEl.getBoundingClientRect = () => ({ top: 0 }) as DOMRect;

  const bubbles = ["user-0", "user-1", "user-2"].map((itemId) => ({ ...bubble, itemId }));
  // Mount only the middle row, at the library's offsets: 200px rows `gap` apart after `scrollMargin`.
  const passThrough = vi.mocked(useVirtualizer).getMockImplementation()!;
  vi.mocked(useVirtualizer).mockImplementation((options) => {
    const start = options.scrollMargin! + 200 + options.gap!;
    const row = {
      index: 1,
      key: options.getItemKey!(1),
      start,
      size: 200,
      end: start + 200,
      lane: 0,
    };
    const totalSize = 3 * 200 + 2 * options.gap!;
    return {
      ...passThrough(options),
      getVirtualItems: () => [row],
      getTotalSize: () => totalSize,
    } as never;
  });
  try {
    const view = render(list(false, scrollEl, vi.fn(), false, bubbles));
    const row = view.container.querySelector<HTMLElement>('[data-index="1"]')!;
    expect(row).toHaveAttribute("data-bubble-key", "user:user-1");
    const wrapper = row.parentElement!;
    expect(wrapper).toHaveStyle("padding-top: 216px; padding-bottom: 216px");
    wrapper.getBoundingClientRect = () => ({ top: 64 }) as DOMRect;

    view.rerender(list(true, scrollEl, vi.fn(), false, bubbles));

    await waitFor(() => expect(vi.mocked(useVirtualizer).mock.lastCall?.[0].scrollMargin).toBe(64));
    expect(wrapper).toHaveStyle("padding-top: 216px; padding-bottom: 216px");
  } finally {
    vi.mocked(useVirtualizer).mockImplementation(passThrough);
  }
});

it("measures rows at their unrounded border-box height", () => {
  const view = render(list(false, document.createElement("div")));
  const row = view.container.querySelector<HTMLElement>('[data-index="0"]')!;
  const measure = vi.mocked(useVirtualizer).mock.lastCall![0].measureElement!;
  const entry = { borderBoxSize: [{ blockSize: 58.796875, inlineSize: 600 }] };
  const instance = {
    options: { getItemKey: () => "user:user-1", horizontal: false },
    indexFromElement: () => 0,
    itemSizeCache: new Map([["user:user-1", 340.625]]),
  };

  expect(measure(row, entry as unknown as ResizeObserverEntry, instance as never)).toBe(58.796875);
  expect(measure(row, undefined, instance as never)).toBe(
    measureElement(row, undefined, instance as never),
  );

  // A first mount has no cached size; read the unrounded box, not offsetHeight.
  instance.itemSizeCache.clear();
  row.getBoundingClientRect = () => ({ height: 58.796875 }) as DOMRect;
  expect(measure(row, undefined, instance as never)).toBe(58.796875);
});

it("publishes navigation that distinguishes loaded and missing turns", async () => {
  const scrollEl = document.createElement("div");
  const onGeometryChange = vi.fn<(geometry: TranscriptGeometry) => void>();

  render(list(false, scrollEl, onGeometryChange));

  await waitFor(() => expect(onGeometryChange).toHaveBeenCalled());
  const geometry = onGeometryChange.mock.calls.at(-1)![0];
  expect(geometry.scrollToItem("user-1")).toBe(true);
  expect(geometry.scrollToItem("missing")).toBe(false);
});

it("recognizes unhandled native find keyboard shortcuts", () => {
  expect(
    isNativeFindShortcut({
      key: "f",
      metaKey: true,
      ctrlKey: false,
      altKey: false,
      defaultPrevented: false,
    }),
  ).toBe(true);
  expect(
    isNativeFindShortcut({
      key: "f",
      metaKey: false,
      ctrlKey: true,
      altKey: false,
      defaultPrevented: false,
    }),
  ).toBe(true);
  expect(
    isNativeFindShortcut({
      key: "f",
      metaKey: true,
      ctrlKey: false,
      altKey: false,
      defaultPrevented: true,
    }),
  ).toBe(false);
});

it("renders every bubble in normal flow after native find is detected", () => {
  const scrollEl = document.createElement("div");

  const view = render(list(false, scrollEl, vi.fn(), true));

  expect(view.container.querySelector('[data-index="0"]')).toBeNull();
  expect(view.container.querySelectorAll('[data-testid="message-bubble"]')).toHaveLength(1);
});

it("keeps actions visible only on a final assistant message while idle", () => {
  const bubbles: Bubble[] = [bubble, assistantBubble, { kind: "compaction", itemId: "compact-1" }];
  const view = render(messageList(bubbles, false, true));
  const copyButtons = screen.getAllByRole("button", { name: "Copy" });

  expect(actionFooter(copyButtons[0]!)).toHaveClass("md:opacity-0");
  expect(actionFooter(copyButtons[1]!)).toHaveClass("opacity-100");
  expect(actionFooter(copyButtons[1]!)).not.toHaveClass(
    "md:opacity-0",
    "md:group-hover:opacity-100",
  );

  view.rerender(messageList(bubbles, false, false));
  expect(actionFooter(screen.getAllByRole("button", { name: "Copy" })[1]!)).toHaveClass(
    "md:opacity-0",
  );
});

it("keeps the final reply's actions hover-only while a follow-up is queued", () => {
  useChatStore.setState({
    queuedMessages: [{ queueId: "q_1", text: "and the tests?", conversationId: "conv-1" }],
  });
  try {
    render(messageList([bubble, assistantBubble], false, true));
    const replyCopy = screen.getAllByRole("button", { name: "Copy" }).at(-1)!;
    expect(actionFooter(replyCopy)).toHaveClass("md:opacity-0");
  } finally {
    useChatStore.setState({ queuedMessages: [] });
  }
});

it.each([{ requiresRetry: true }, { waitingForHost: true }])(
  "keeps the final reply's actions visible while a queued follow-up cannot send (%o)",
  (blocked) => {
    useChatStore.setState({
      queuedMessages: [
        { queueId: "q_1", text: "and the tests?", conversationId: "conv-1", ...blocked },
      ],
    });
    try {
      render(messageList([bubble, assistantBubble], false, true));
      const replyCopy = screen.getAllByRole("button", { name: "Copy" }).at(-1)!;
      expect(actionFooter(replyCopy)).toHaveClass("opacity-100");
    } finally {
      useChatStore.setState({ queuedMessages: [] });
    }
  },
);

it("keeps every action row hover-only when the final message is from the user", () => {
  render(messageList([assistantBubble, bubble], false, true));

  for (const button of screen.getAllByRole("button", { name: "Copy" })) {
    expect(actionFooter(button)).toHaveClass("md:opacity-0");
  }
});

it("toggles mobile message actions on tap and shows the latest assistant's actions by default", async () => {
  const user = userEvent.setup();
  const mediaQuery = window.matchMedia("(max-width: 767.98px)");
  const matchMedia = vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    ...mediaQuery,
    media: query,
    matches: query === "(max-width: 767.98px)",
  }));
  try {
    const olderReply = {
      ...assistantBubble,
      items: [
        { kind: "text" as const, itemId: "assistant-1", text: "[Details](#details)", final: true },
      ],
    };
    const latestReply = { ...assistantBubble, responseId: "response-2", stableId: "response-2" };
    const followUp = { ...bubble, itemId: "user-2" };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(messageList([bubble, olderReply, latestReply, followUp], true, false), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    const footers = screen.getAllByRole("button", { name: "Copy" }).map(actionFooter);

    expect(footers[0]).toHaveClass("invisible", "opacity-0");
    expect(footers[1]).toHaveClass("invisible", "opacity-0");
    expect(footers[2]).toHaveClass("opacity-100");
    expect(footers[2]).not.toHaveClass("invisible");
    expect(footers[3]).toHaveClass("invisible", "opacity-0");

    const olderMessage = view.container.querySelector<HTMLElement>(
      '[data-message-id="response-1"]',
    )!;
    fireEvent.click(within(olderMessage).getByRole("link", { name: "Details" }));
    expect(footers[1]).toHaveClass("invisible");
    const reveal = within(olderMessage).getByRole("button", { name: "Show message actions" });
    expect(document.getElementById(reveal.getAttribute("aria-controls")!)).toBe(footers[1]);
    reveal.focus();
    await user.keyboard("{Enter}");
    expect(reveal).toHaveAttribute("aria-expanded", "true");
    expect(footers[1]).not.toHaveClass("invisible");
    await user.click(within(olderMessage).getByRole("button", { name: "Copy" }));
    expect(footers[1]).not.toHaveClass("invisible");
    reveal.focus();
    await user.keyboard(" ");
    expect(reveal).toHaveAttribute("aria-expanded", "false");
    expect(footers[1]).toHaveClass("invisible");

    fireEvent.click(view.container.querySelector('[data-message-id="user-1"]')!);
    fireEvent.click(view.container.querySelector('[data-message-id="response-1"]')!);
    expect(footers[0]).not.toHaveClass("invisible");
    expect(footers[1]).not.toHaveClass("invisible");
    expect(footers[3]).toHaveClass("invisible");
    fireEvent.click(view.container.querySelector('[data-message-id="user-1"]')!);
    fireEvent.click(view.container.querySelector('[data-message-id="response-1"]')!);
    expect(footers[0]).toHaveClass("invisible");
    expect(footers[1]).toHaveClass("invisible");

    view.rerender(
      messageList(
        [
          bubble,
          olderReply,
          latestReply,
          followUp,
          { ...assistantBubble, responseId: "response-3", stableId: "response-3" },
        ],
        false,
        true,
      ),
    );
    const updatedFooters = screen.getAllByRole("button", { name: "Copy" }).map(actionFooter);
    expect(updatedFooters[2]).toHaveClass("invisible");
    expect(updatedFooters[4]).toHaveClass("opacity-100");
    expect(updatedFooters[4]).not.toHaveClass("invisible");
    fireEvent.click(view.container.querySelector('[data-message-id="response-3"]')!);
    expect(updatedFooters[4]).toHaveClass("invisible");
    fireEvent.click(view.container.querySelector('[data-message-id="response-3"]')!);
    expect(updatedFooters[4]).not.toHaveClass("invisible");
  } finally {
    matchMedia.mockRestore();
  }
});

it("keeps a renamed top bubble's row across a history prepend", () => {
  // An assistant bubble is keyed by its first item, so a page that continues
  // the top turn renames it. The row must keep its node: a remount replays the
  // action row's hover fade on every page while older history loads.
  const scrollEl = document.createElement("div");
  Object.defineProperties(scrollEl, {
    scrollTop: { configurable: true, writable: true, value: 0 },
    clientHeight: { configurable: true, value: 500 },
    scrollHeight: { configurable: true, value: 1_000 },
  });
  scrollEl.getBoundingClientRect = () => ({ top: 0 }) as DOMRect;
  const transcript = (bubbles: Bubble[]) => (
    <Conversation>
      <ConversationContent>
        <VirtualBubbleList
          bubbles={bubbles}
          scrollEl={scrollEl}
          lastAssistantIndex={bubbles.length - 1}
          showsWorking={false}
          sessionIdle
          conversationId="conv-1"
          hasTasks={false}
          disableVirtualization={false}
          onGeometryChange={vi.fn()}
        />
      </ConversationContent>
    </Conversation>
  );

  const view = render(transcript([assistantBubble]));
  const row = view.container.querySelector<HTMLElement>('[data-index="0"]')!;
  expect(row).toHaveAttribute("data-bubble-key", "assistant:response-1");

  // The page lands an earlier item of the same response: same bubble, new key.
  const renamed: Bubble = {
    ...assistantBubble,
    stableId: "assistant-0",
    items: [
      { kind: "text", itemId: "assistant-0", text: "hello first", final: true },
      ...assistantBubble.items,
    ],
  };
  view.rerender(transcript([renamed]));

  expect(view.container.querySelector('[data-index="0"]')).toBe(row);
  expect(row).toHaveAttribute("data-bubble-key", "assistant:response-1");

  // A genuinely earlier turn is a new row above; the renamed bubble keeps its node.
  view.rerender(transcript([bubble, renamed]));
  expect(view.container.querySelector('[data-index="1"]')).toBe(row);
  expect(view.container.querySelector('[data-index="0"]')).toHaveAttribute(
    "data-bubble-key",
    "user:user-1",
  );
});
