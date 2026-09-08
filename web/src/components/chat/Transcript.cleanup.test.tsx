import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Conversation, ConversationContent } from "@/components/ai-elements/conversation";
import { useChatStore } from "@/store/chatStore";
import { VirtualBubbleList } from "./Transcript";

afterEach(cleanup);

it("does not read incoming layout while flushing the outgoing view on cleanup", async () => {
  useChatStore.setState({ conversationId: "conv-cleanup" });
  const scrollEl = document.createElement("div");
  let scrollHeightReads = 0;
  Object.defineProperties(scrollEl, {
    scrollHeight: {
      configurable: true,
      get: () => {
        scrollHeightReads += 1;
        return 1_000;
      },
    },
    clientHeight: { configurable: true, value: 500 },
    scrollTop: { configurable: true, writable: true, value: 500 },
  });

  const { unmount } = render(
    <Conversation>
      <ConversationContent>
        <VirtualBubbleList
          bubbles={[]}
          scrollEl={scrollEl}
          lastAssistantIndex={-1}
          showsWorking={false}
          conversationId="conv-cleanup"
          hasTasks={false}
          disableVirtualization={false}
          onGeometryChange={vi.fn()}
        />
      </ConversationContent>
    </Conversation>,
  );
  await new Promise<void>((resolve) => {
    setTimeout(resolve, 50);
  });
  fireEvent.scroll(scrollEl);
  await waitFor(() => expect(scrollHeightReads).toBeGreaterThan(0));
  scrollHeightReads = 0;

  unmount();

  expect(scrollHeightReads).toBe(0);
});
