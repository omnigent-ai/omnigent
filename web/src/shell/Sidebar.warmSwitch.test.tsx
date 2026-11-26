// The sidebar row's switch-first wiring: with `warm_switch_keep_mounted_ui` on,
// a plain click on a session row switches the chat store as it navigates; a
// modified click (new tab) and the flag-off path never touch the store.
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as capabilities from "@/lib/capabilities";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { sessionRowsPage } from "@/lib/sidebarData";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useChatStore } from "@/store/chatStore";
import { renderSidebar } from "@/test/sidebarTestHelpers";
import type { Conversation } from "@/hooks/useConversations";

const initialStoreState = useChatStore.getState();
const fetchMock = vi.fn();

function row(id: string): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    labels: {},
    created_at: 1,
    updated_at: 1,
    permission_level: 4,
  };
}

function seedLive(id: string) {
  const entry = conversationRegistry.acquire(id);
  entry.setState({ abortController: new AbortController(), blocks: [] });
}

afterEach(() => {
  cleanup();
  useChatStore.getState().abortController?.abort();
  conversationRegistry.clear();
  useChatStore.setState(initialStoreState);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Sidebar warm switch activation", () => {
  it("switches the store while navigating on a plain click with the flag on", async () => {
    vi.spyOn(capabilities, "getCachedServerInfo").mockReturnValue({
      ...FALLBACK_SERVER_INFO,
      features: { warm_switch_keep_mounted_ui: true },
    });
    useChatStore.setState({ conversationId: "conv_a" });
    seedLive("conv_a");
    seedLive("conv_b");
    fetchMock.mockImplementation((url: string) => {
      const body = String(url).includes("/projects")
        ? []
        : sessionRowsPage([row("conv_a"), row("conv_b")]);
      return Promise.resolve({ ok: true, json: async () => body });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSidebar({ route: "/c/conv_a", features: { warm_switch_keep_mounted_ui: true } });
    await screen.findByText("conv_b");

    act(() => fireEvent.click(screen.getByText("conv_b")));

    expect(useChatStore.getState().conversationId).toBe("conv_b");
  });

  it("leaves the store on a modified click that opens a new tab", async () => {
    vi.spyOn(capabilities, "getCachedServerInfo").mockReturnValue({
      ...FALLBACK_SERVER_INFO,
      features: { warm_switch_keep_mounted_ui: true },
    });
    useChatStore.setState({ conversationId: "conv_a" });
    seedLive("conv_a");
    seedLive("conv_b");
    fetchMock.mockImplementation((url: string) => {
      const body = String(url).includes("/projects")
        ? []
        : sessionRowsPage([row("conv_a"), row("conv_b")]);
      return Promise.resolve({ ok: true, json: async () => body });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSidebar({ route: "/c/conv_a", features: { warm_switch_keep_mounted_ui: true } });
    await screen.findByText("conv_b");

    act(() => fireEvent.click(screen.getByText("conv_b"), { metaKey: true }));

    expect(useChatStore.getState().conversationId).toBe("conv_a");
  });

  it("does not switch the store when the flag is off", async () => {
    useChatStore.setState({ conversationId: "conv_a" });
    seedLive("conv_a");
    seedLive("conv_b");
    fetchMock.mockImplementation((url: string) => {
      const body = String(url).includes("/projects")
        ? []
        : sessionRowsPage([row("conv_a"), row("conv_b")]);
      return Promise.resolve({ ok: true, json: async () => body });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSidebar({ route: "/c/conv_a" });
    await screen.findByText("conv_b");

    act(() => fireEvent.click(screen.getByText("conv_b")));

    expect(useChatStore.getState().conversationId).toBe("conv_a");
  });
});
