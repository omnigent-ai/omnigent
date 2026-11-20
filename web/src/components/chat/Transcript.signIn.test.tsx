import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import type { UserMessageBlock } from "@/lib/blocks";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { getSessionSignInLink } from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
import { Transcript } from "./Transcript";

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionSignInLink: vi.fn(),
}));

afterEach(() => {
  cleanup();
  vi.mocked(getSessionSignInLink).mockReset();
});

const userTurn: UserMessageBlock = {
  type: "user_message",
  ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "resp_1", itemId: "item_1" },
  content: [{ type: "input_text", text: "Summarize the README." }],
};

it.each([
  { name: "an empty native chat", native: true, blocks: [], shown: true },
  { name: "an empty non-native chat", native: false, blocks: [], shown: false },
  { name: "a native chat with messages", native: true, blocks: [userTurn], shown: false },
])("offers a pending sign-in in $name: $shown", async ({ native, blocks, shown }) => {
  vi.mocked(getSessionSignInLink).mockResolvedValue({
    pending: true,
    url: "https://signin.example.com/device",
    code: null,
  });
  useChatStore.setState({
    conversationId: "conv_sign_in",
    isNativeTerminalSession: native,
    blocks,
  });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <Transcript
          hostId={null}
          setConversationEl={vi.fn()}
          containerEl={null}
          scroller={null}
          setScroller={vi.fn()}
          sendScrollNonce={0}
          hasMoreHistory={false}
          loadingMoreHistory={false}
          isMobileViewport={false}
          showsWorking={false}
          agentsError={null}
          sandboxLaunching={false}
          spacerMeasureRef={{ current: null }}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  if (shown) {
    expect(await screen.findByRole("button", { name: "Open sign-in link" })).toBeInTheDocument();
  } else {
    expect(screen.queryByRole("button", { name: "Open sign-in link" })).toBeNull();
    expect(getSessionSignInLink).not.toHaveBeenCalled();
  }
});
