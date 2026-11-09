import { act, cleanup, render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import { MemoryRouter, Route, Routes, useNavigate, type NavigateFunction } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "@/shell/TerminalFirstContext";
import { useChatStore } from "@/store/chatStore";
import { ChatPage } from "./ChatPage";

// The warm terminal-surface cache keeps a recently viewed session's
// MainTerminalView mounted after navigating away, so switching back reveals
// the live terminal attach instead of re-dialing it. These tests render the
// real ChatPage and record every MainTerminalView mount/unmount: a surface
// that unmounts on a session switch has dropped its WebSocket.
const lifecycle: string[] = [];
vi.mock("@/shell/MainTerminalView", async () => {
  const React = await import("react");
  return {
    MainTerminalView: ({ conversationId }: { conversationId: string }) => {
      React.useEffect(() => {
        lifecycle.push(`mount:${conversationId}`);
        return () => {
          lifecycle.push(`unmount:${conversationId}`);
        };
      }, [conversationId]);
      return <div data-testid={`terminal-${conversationId}`} />;
    },
  };
});

const terminalFirst = {
  isClaudeNative: true,
  isNativeWrapper: true,
  isTerminalFirst: true,
  isShellView: false,
  view: "terminal",
  terminalViewKey: null,
  setView: () => {},
  terminalsAvailable: true,
  terminalStartingUp: false,
} as TerminalFirstContextValue;

let navigateRef: NavigateFunction | null = null;
function NavigateCapture() {
  const navigate = useNavigate();
  useEffect(() => {
    navigateRef = navigate;
  }, [navigate]);
  return null;
}

function renderChatPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <SidebarDataProvider>
        <TooltipProvider>
          <TerminalFirstContextProvider value={terminalFirst}>
            <MemoryRouter initialEntries={["/c/conv_a"]}>
              <NavigateCapture />
              <Routes>
                <Route path="/c/:conversationId" element={<ChatPage />} />
              </Routes>
            </MemoryRouter>
          </TerminalFirstContextProvider>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

async function navigateTo(path: string) {
  await act(async () => {
    navigateRef!(path);
  });
  await act(async () => {});
}

afterEach(() => {
  cleanup();
  lifecycle.length = 0;
  useChatStore.setState({ conversationId: null, loadingConversation: false });
});

describe("warm terminal surfaces across session switches", () => {
  it("keeps a live session's terminal mounted while another session is open", async () => {
    // Every session is already live in the store: switchTo settles at once
    // with no snapshot load, the best case for the cache.
    useChatStore.setState({
      switchTo: async (id: string | null) => {
        useChatStore.setState({ conversationId: id, loadingConversation: false });
      },
    } as never);
    renderChatPage();
    await act(async () => {});
    expect(lifecycle).toEqual(["mount:conv_a"]);

    await navigateTo("/c/conv_b");
    await navigateTo("/c/conv_a");

    expect(lifecycle).toEqual(["mount:conv_a", "mount:conv_b"]);
  });

  it("keeps a live session's terminal mounted across a cold switch that loads a snapshot", async () => {
    // The incoming session is not live: switchTo shows the hydrating
    // placeholder until its snapshot resolves.
    const pending: (() => void)[] = [];
    const settle = () => {
      pending.splice(0).forEach((resolve) => resolve());
    };
    useChatStore.setState({
      switchTo: async (id: string | null) => {
        if (useChatStore.getState().conversationId === id) return;
        // Like the real switchTo: the id flips at once, the entry loads after.
        useChatStore.setState({ conversationId: id });
        useChatStore.setState({ loadingConversation: true });
        await new Promise<void>((resolve) => {
          pending.push(resolve);
        });
        useChatStore.setState({ loadingConversation: false });
      },
    } as never);
    renderChatPage();
    await act(async () => {
      settle();
    });
    await act(async () => {});
    expect(lifecycle).toEqual(["mount:conv_a"]);

    await navigateTo("/c/conv_b");
    // conv_a's surface must survive the placeholder shown while conv_b loads.
    expect(lifecycle).toEqual(["mount:conv_a"]);
    await act(async () => {
      settle();
    });
    await act(async () => {});
    expect(lifecycle).toEqual(["mount:conv_a", "mount:conv_b"]);

    await navigateTo("/c/conv_a");
    expect(lifecycle).toEqual(["mount:conv_a", "mount:conv_b"]);
  });
});
