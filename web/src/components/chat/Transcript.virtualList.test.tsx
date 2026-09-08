import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { Bubble } from "@/lib/renderItems";
import { Conversation, ConversationContent } from "@/components/ai-elements/conversation";
import { isNativeFindShortcut, type TranscriptGeometry, VirtualBubbleList } from "./Transcript";

afterEach(cleanup);

const bubble: Extract<Bubble, { kind: "user" }> = {
  kind: "user",
  itemId: "user-1",
  content: [{ type: "input_text", text: "hello" }],
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
          conversationId={undefined}
          hasTasks={hasTasks}
          disableVirtualization={disableVirtualization}
          onGeometryChange={onGeometryChange}
        />
      </ConversationContent>
    </Conversation>
  );
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
