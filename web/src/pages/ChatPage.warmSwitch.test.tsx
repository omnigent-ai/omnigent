// Warm session switches with `warm_switch_keep_mounted_ui`: a sidebar-style
// click switches the store with the navigation, so the commit landing
// `/c/:id` skips the hydrating placeholder and the chat surface stays mounted.
import { useLayoutEffect } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Link, MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { UserMessageBlock } from "@/lib/blocks";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import * as capabilities from "@/lib/capabilities";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import {
  clearWarmSwitchInFlight,
  switchSessionBeforeNavigate,
  warmSwitchInFlight,
} from "@/lib/warmSessionSwitch";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { conversationRegistry } from "@/store/conversationRegistry";
import {
  consumePendingInitialPrompt,
  initChatStore,
  setPendingInitialPrompt,
  useChatStore,
} from "@/store/chatStore";
import { clearSessionDrafts } from "@/lib/sessionDrafts";
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
    boundAgentId: "agent_example",
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
  useLayoutEffect(() => {
    commitLog.push({ urlConvId, activeConversationId, loadingConversation });
  });
  return null;
}

/** The sidebar row's click shape: switch first, then let the Link navigate. */
function SessionLink({
  id,
  label,
  cancelNavigation = false,
}: {
  id: string;
  label: string;
  cancelNavigation?: boolean;
}) {
  const { conversationId } = useParams<{ conversationId: string }>();
  return (
    <Link
      to={`/c/${id}`}
      onClick={(event) => {
        switchSessionBeforeNavigate(id, conversationId ?? null);
        if (cancelNavigation) event.preventDefault();
      }}
    >
      {label}
    </Link>
  );
}

function renderPage(
  features: Record<string, boolean>,
  qc = new QueryClient(),
  cancelBetaNavigation = false,
) {
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
                    <SessionLink id="conv_a" label="open alpha" />
                    <SessionLink
                      id="conv_b"
                      label="open beta"
                      cancelNavigation={cancelBetaNavigation}
                    />
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
  clearWarmSwitchInFlight();
  clearSessionDrafts();
  consumePendingInitialPrompt("conv_a");
  useChatStore.setState(initialStoreState);
  commitLog.length = 0;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
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

  it("restores the URL's session when a later handler cancels navigation", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    seedLiveEntry("conv_b", "beta transcript");
    renderPage({ warm_switch_keep_mounted_ui: true }, qc, true);
    await screen.findByText("alpha transcript");
    const surfaceBefore = transcriptNode();
    vi.useFakeTimers();

    fireEvent.click(screen.getByRole("link", { name: "open beta" }));

    expect(commitLog.at(-1)).toMatchObject({ urlConvId: "conv_a", activeConversationId: "conv_b" });
    expect(screen.getByText("beta transcript")).toBeInTheDocument();
    expect(transcriptNode()).toBe(surfaceBefore);
    await act(() => vi.advanceTimersByTimeAsync(1000));

    expect(useChatStore.getState().conversationId).toBe("conv_a");
    expect(screen.getByText("alpha transcript")).toBeInTheDocument();
    expect(screen.queryByText("Loading conversation…")).toBeNull();
    expect(warmSwitchInFlight("conv_b", "conv_a")).toBe(false);
  });

  it.each([null, { name: "example-skill", args: "check" }])(
    "holds cold A's initial prompt across a warm switch and pins delivery on return: %o",
    async (skill) => {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      initChatStore(qc);
      const alpha = seedLiveEntry("conv_a", "alpha transcript");
      seedLiveEntry("conv_b", "beta transcript");
      alpha.setState({ loadingConversation: true });
      const send = vi.fn().mockResolvedValue(undefined);
      const sendSlashCommand = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ send, sendSlashCommand, loadingConversation: true });
      setPendingInitialPrompt("conv_a", { text: "alpha private prompt", skill });
      renderPage({ warm_switch_keep_mounted_ui: true }, qc);
      expect(screen.getByText("Loading conversation…")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("link", { name: "open beta" }));

      expect(screen.getByText("beta transcript")).toBeInTheDocument();
      expect(send).not.toHaveBeenCalled();
      expect(sendSlashCommand).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("link", { name: "open alpha" }));
      expect(send).not.toHaveBeenCalled();
      expect(sendSlashCommand).not.toHaveBeenCalled();
      act(() => alpha.setState({ loadingConversation: false }));

      await waitFor(() => expect(skill ? sendSlashCommand : send).toHaveBeenCalledTimes(1));
      if (skill) {
        expect(sendSlashCommand).toHaveBeenCalledWith(skill.name, skill.args, "agent_example", {
          pinnedConversationId: "conv_a",
        });
        expect(send).not.toHaveBeenCalled();
      } else {
        expect(send).toHaveBeenCalledWith("alpha private prompt", "agent_example", [], {
          pinnedConversationId: "conv_a",
        });
        expect(sendSlashCommand).not.toHaveBeenCalled();
      }
      fireEvent.click(screen.getByRole("link", { name: "open beta" }));
      fireEvent.click(screen.getByRole("link", { name: "open alpha" }));
      expect(skill ? sendSlashCommand : send).toHaveBeenCalledTimes(1);
    },
  );

  it("clears command and attachment errors on a mounted session switch", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    seedLiveEntry("conv_b", "beta transcript");
    for (const id of ["conv_a", "conv_b"]) {
      qc.setQueryData(["session", id], {
        id,
        agentId: "agent_example",
        permissionLevel: 4,
        status: "idle",
        labels: {},
      });
    }
    useChatStore.setState({ boundAgentId: "agent_example" });
    renderPage({ warm_switch_keep_mounted_ui: true }, qc);
    let rejectModel!: (error: Error) => void;
    const setModel = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectModel = reject;
        }),
    );
    useChatStore.setState({ setModel });
    const input = await screen.findByRole("textbox");
    fireEvent.change(input, { target: { value: "/model example-model" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(setModel).toHaveBeenCalledOnce();
    fireEvent.change(input, { target: { value: "/new unexpected" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByText("/new does not accept arguments")).toBeInTheDocument();
    fireEvent.change(document.querySelector('input[type="file"]')!, {
      target: { files: [new File(["audio"], "recording.wav", { type: "audio/wav" })] },
    });
    expect(screen.getByText(/can't be attached/)).toBeInTheDocument();
    const surfaceBefore = transcriptNode();

    fireEvent.click(screen.getByRole("link", { name: "open beta" }));

    expect(transcriptNode()).toBe(surfaceBefore);
    expect(screen.queryByText("/new does not accept arguments")).toBeNull();
    expect(screen.queryByText(/can't be attached/)).toBeNull();
    await act(async () => rejectModel(new Error("alpha model update failed")));
    expect(screen.queryByText("alpha model update failed")).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: "open alpha" }));
    expect(screen.queryByText("alpha model update failed")).toBeNull();
  });
});
