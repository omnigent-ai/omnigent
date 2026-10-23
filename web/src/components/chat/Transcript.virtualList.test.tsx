import { cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { Bubble } from "@/lib/renderItems";
import { Conversation, ConversationContent } from "@/components/ai-elements/conversation";
import {
  isNativeFindShortcut,
  findShortcutRoute,
  useChatFind,
  useChatFindShortcut,
  type TranscriptGeometry,
  VirtualBubbleList,
} from "./Transcript";
import { FILE_FIND_OWNER_ATTRIBUTE, isMacPlatform, requestChatFind } from "@/lib/hotkeys";

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

function list(
  hasTasks: boolean,
  scrollEl: HTMLElement,
  onGeometryChange: (geometry: TranscriptGeometry) => void = vi.fn(),
  disableVirtualization = false,
) {
  return (
    <Conversation>
      <ConversationContent>
        <VirtualBubbleList
          bubbles={[bubble]}
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

  const view = render(list(false, scrollEl));
  const row = view.container.querySelector<HTMLElement>('[data-index="0"]')!;
  expect(row).toHaveAttribute("data-bubble-key", "user:user-1");
  const wrapper = row.parentElement!;
  wrapper.getBoundingClientRect = () => ({ top: 64 }) as DOMRect;

  view.rerender(list(true, scrollEl));

  await waitFor(() => expect(row.style.transform).toBe("translateY(-64px)"));
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
      shiftKey: false,
      defaultPrevented: false,
    }),
  ).toBe(true);
  expect(
    isNativeFindShortcut({
      key: "f",
      metaKey: false,
      ctrlKey: true,
      altKey: false,
      shiftKey: false,
      defaultPrevented: false,
    }),
  ).toBe(true);
  expect(
    isNativeFindShortcut({
      key: "f",
      metaKey: true,
      ctrlKey: false,
      altKey: false,
      shiftKey: false,
      defaultPrevented: true,
    }),
  ).toBe(false);
});

it("routes ⌘F to the chat's find, the browser's find or an open file", () => {
  document.body.innerHTML = `<input id="find" />`;
  const findInput = document.getElementById("find");
  const press = {
    key: "f",
    metaKey: isMacPlatform(),
    ctrlKey: !isMacPlatform(),
    altKey: false,
    shiftKey: false,
    defaultPrevented: false,
  };
  const options = { showTerminal: false, findInput, fileOwnsFind: false };

  expect(findShortcutRoute(press, options)).toBe("chat");
  expect(findShortcutRoute(press, { ...options, showTerminal: true })).toBe("native");
  // An open file that owns ⌘F gets it untouched: no find bar, no native-find render.
  expect(findShortcutRoute(press, { ...options, fileOwnsFind: true })).toBeNull();
  expect(findShortcutRoute({ ...press, key: "g" }, options)).toBeNull();
  // ⌘⇧F belongs to the workspace's content search.
  expect(findShortcutRoute({ ...press, shiftKey: true }, options)).toBeNull();
  findInput?.focus();
  expect(findShortcutRoute(press, options)).toBe("native");
  document.body.innerHTML = "";
});

it("claims ⌘F ahead of the side panel's listeners unless an open file owns it", () => {
  document.body.innerHTML = `<input id="find" /><aside data-testid="file-viewer"></aside>`;
  const findInputRef = { current: document.getElementById("find") as HTMLInputElement };
  const onChatFind = vi.fn();
  const onNativeFind = vi.fn();
  // Registered before the chat's, as a file viewer opened earlier would be.
  const panelSaw: boolean[] = [];
  const panel = (event: KeyboardEvent) => panelSaw.push(event.defaultPrevented);
  window.addEventListener("keydown", panel);
  const { unmount } = renderHook(() =>
    useChatFindShortcut({
      enabled: true,
      showTerminal: false,
      findInputRef,
      onChatFind,
      onNativeFind,
    }),
  );
  const press = () =>
    document.body.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "f",
        metaKey: isMacPlatform(),
        ctrlKey: !isMacPlatform(),
        bubbles: true,
        cancelable: true,
      }),
    );

  press();
  expect(onChatFind).toHaveBeenCalledOnce();
  expect(panelSaw).toEqual([true]);

  document.querySelector("aside")?.setAttribute(FILE_FIND_OWNER_ATTRIBUTE, "");
  press();
  expect(onChatFind).toHaveBeenCalledOnce();
  expect(onNativeFind).not.toHaveBeenCalled();
  expect(panelSaw).toEqual([true, false]);

  unmount();
  window.removeEventListener("keydown", panel);
  document.body.innerHTML = "";
});

it("opens the chat's find bar when the header asks, outside the terminal view", () => {
  const onChatFind = vi.fn();
  const findInputRef = { current: null };
  const { rerender, unmount } = renderHook(
    ({ showTerminal }) =>
      useChatFindShortcut({
        enabled: true,
        showTerminal,
        findInputRef,
        onChatFind,
        onNativeFind: vi.fn(),
      }),
    { initialProps: { showTerminal: false } },
  );

  requestChatFind();
  expect(onChatFind).toHaveBeenCalledTimes(1);

  rerender({ showTerminal: true });
  requestChatFind();
  expect(onChatFind).toHaveBeenCalledTimes(1);

  unmount();
  requestChatFind();
  expect(onChatFind).toHaveBeenCalledTimes(1);
});

it("focuses the find input within the tap that opens it", () => {
  let focusedDuringTap: Element | null = null;
  function Harness() {
    const { find, findInputRef, openChatFind } = useChatFind("conv-1", null);
    return (
      <>
        <button
          type="button"
          onClick={() => {
            openChatFind();
            focusedDuringTap = document.activeElement;
          }}
        >
          open
        </button>
        {find ? <input aria-label="find" ref={findInputRef} /> : null}
      </>
    );
  }
  render(<Harness />);

  screen.getByRole("button", { name: "open" }).click();

  expect(focusedDuringTap).toBe(screen.getByRole("textbox", { name: "find" }));
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

it("keeps every action row hover-only when the final message is from the user", () => {
  render(messageList([assistantBubble, bubble], false, true));

  for (const button of screen.getAllByRole("button", { name: "Copy" })) {
    expect(actionFooter(button)).toHaveClass("md:opacity-0");
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
