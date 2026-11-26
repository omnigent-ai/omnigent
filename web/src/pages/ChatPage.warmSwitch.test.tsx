// Warm session switches with `warm_switch_keep_mounted_ui`: a sidebar-style
// click switches the store with the navigation, so the commit landing
// `/c/:id` skips the hydrating placeholder and the chat surface stays mounted.
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Link, MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { UserMessageBlock } from "@/lib/blocks";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import * as capabilities from "@/lib/capabilities";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { switchSessionBeforeNavigate } from "@/lib/warmSessionSwitch";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { conversationRegistry } from "@/store/conversationRegistry";
import { initChatStore, useChatStore } from "@/store/chatStore";
import { ChatPage } from "./ChatPage";

const initialStoreState = useChatStore.getState();

/** A committed user turn, so each conversation's transcript has visible text. */
function userBlock(id: string, text: string): UserMessageBlock {
  return {
    type: "user_message",
    ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: id, itemId: id },
    content: [{ type: "input_text", text }],
  };
}

/** A retained entry with an open stream — what a warm switch paints from. */
function seedLiveEntry(id: string, text: string) {
  const entry = conversationRegistry.acquire(id);
  entry.setState({
    abortController: new AbortController(),
    blocks: [userBlock(id, text)],
  });
  return entry;
}

/** The chat surface's scroll container; a remount replaces the DOM node. */
const transcriptNode = () => document.querySelector(".session-chat-transcript");

interface CommitShape {
  urlConvId: string | undefined;
  activeConversationId: string | null;
  loadingConversation: boolean;
}
const commitLog: CommitShape[] = [];

/** Records what each commit of the route saw, so placeholder commits are observable. */
function RouteCommitLog() {
  const { conversationId: urlConvId } = useParams<{ conversationId: string }>();
  const activeConversationId = useChatStore((s) => s.conversationId);
  const loadingConversation = useChatStore((s) => s.loadingConversation);
  commitLog.push({ urlConvId, activeConversationId, loadingConversation });
  return null;
}

/** The sidebar row's click shape: switch first, then let the Link navigate. */
function SessionLink({ id, label }: { id: string; label: string }) {
  return (
    <Link to={`/c/${id}`} onClick={() => switchSessionBeforeNavigate(id)}>
      {label}
    </Link>
  );
}

function renderPage(features: Record<string, boolean>, qc = new QueryClient()) {
  vi.spyOn(capabilities, "getCachedServerInfo").mockReturnValue({
    ...FALLBACK_SERVER_INFO,
    features,
  });
  render(
    <QueryClientProvider client={qc}>
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, features }}>
        <SidebarDataProvider>
          <MemoryRouter initialEntries={["/c/conv_a"]}>
            <Routes>
              <Route
                path="c/:conversationId"
                element={
                  <>
                    <RouteCommitLog />
                    <SessionLink id="conv_b" label="open beta" />
                    <SessionLink id="conv_c" label="open gamma" />
                    <ChatPage />
                  </>
                }
              />
            </Routes>
          </MemoryRouter>
        </SidebarDataProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useChatStore.setState({
    conversationId: "conv_a",
    blocks: [userBlock("item_a", "alpha transcript")],
    loadingConversation: false,
    conversationLoadError: null,
  });
});

afterEach(() => {
  cleanup();
  useChatStore.getState().abortController?.abort();
  conversationRegistry.clear();
  useChatStore.setState(initialStoreState);
  commitLog.length = 0;
  vi.restoreAllMocks();
});

describe("ChatPage warm switch (warm_switch_keep_mounted_ui)", () => {
  it("flag on, live target: the surface stays mounted and no placeholder renders", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    seedLiveEntry("conv_b", "beta transcript");
    renderPage({ warm_switch_keep_mounted_ui: true }, qc);
    await waitFor(() => expect(document.body.textContent).toContain("alpha transcript"));
    const surfaceBefore = transcriptNode();
    expect(surfaceBefore).not.toBeNull();
    commitLog.length = 0;

    act(() => fireEvent.click(screen.getByRole("link", { name: "open beta" })));

    expect(useChatStore.getState().conversationId).toBe("conv_b");
    expect(screen.queryByText("Loading conversation…")).toBeNull();
    expect(transcriptNode()).toBe(surfaceBefore);
    await waitFor(() => expect(document.body.textContent).toContain("beta transcript"));
    // The store write re-renders one frame before the URL's transition commit
    // (external-store updates are synchronous); the gate holds the surface
    // through that window, so every commit after the click shows the target.
    expect(commitLog.every((commit) => commit.activeConversationId === "conv_b")).toBe(true);
    expect(commitLog).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ urlConvId: "conv_b", activeConversationId: "conv_b" }),
      ]),
    );
  });

  it("flag off: the placeholder commits and the surface remounts as before", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    seedLiveEntry("conv_b", "beta transcript");
    renderPage({}, qc);
    await waitFor(() => expect(document.body.textContent).toContain("alpha transcript"));
    const surfaceBefore = transcriptNode();
    commitLog.length = 0;

    act(() => fireEvent.click(screen.getByRole("link", { name: "open beta" })));

    expect(useChatStore.getState().conversationId).toBe("conv_b");
    // The mismatch commit rendered the placeholder and tore the surface down.
    expect(commitLog).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          urlConvId: "conv_b",
          activeConversationId: "conv_a",
          loadingConversation: false,
        }),
      ]),
    );
    expect(transcriptNode()).not.toBe(surfaceBefore);
    await waitFor(() => expect(document.body.textContent).toContain("beta transcript"));
  });

  it("cold target still shows the placeholder with the flag on", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    // conv_c was never opened: no live entry, so the switch must cold-bind and
    // keep the hydrating placeholder until its snapshot lands. The never-settling
    // fetch pins that state for the assertion.
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    renderPage({ warm_switch_keep_mounted_ui: true }, qc);
    await waitFor(() => expect(document.body.textContent).toContain("alpha transcript"));
    const surfaceBefore = transcriptNode();

    act(() => fireEvent.click(screen.getByRole("link", { name: "open gamma" })));

    expect(useChatStore.getState().conversationId).toBe("conv_c");
    expect(useChatStore.getState().loadingConversation).toBe(true);
    expect(screen.getByText("Loading conversation…")).toBeInTheDocument();
    expect(transcriptNode()).not.toBe(surfaceBefore);
  });
});
