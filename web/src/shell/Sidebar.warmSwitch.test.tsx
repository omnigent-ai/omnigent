// The sidebar row's switch-first wiring: a plain click on a session row switches
// the chat store as it navigates; a modified click (new tab) never touches the store.
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { useLayoutEffect } from "react";
import { useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { sessionRowsPage } from "@/lib/sidebarData";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useChatStore } from "@/store/chatStore";
import type { Conversation } from "@/hooks/useConversations";
import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversation } from "@/test/sidebarMockHelpers";

const initialStoreState = useChatStore.getState();

function LocationProbe() {
  const location = useLocation();
  useLayoutEffect(() => {
    window.history.replaceState({}, "", location.pathname + location.search);
  }, [location]);
  return (
    <output data-testid="location">
      {location.pathname}
      {location.search}
    </output>
  );
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

function renderSessions(sessions = [conversation("conv_a"), conversation("conv_b")]) {
  useChatStore.setState({ conversationId: "conv_a" });
  if (!sessions.some((session) => session.id === "conv_a")) seedLive("conv_a");
  for (const session of sessions) seedLive(session.id);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => ({
      ok: true,
      json: async () => (String(url).includes("/projects") ? [] : sessionRowsPage(sessions)),
    })),
  );
  return renderSidebar({ route: "/c/conv_a", features: {}, children: <LocationProbe /> });
}

describe("Sidebar warm switch activation", () => {
  it("switches the store while navigating on a plain click", async () => {
    renderSessions();

    fireEvent.click(await screen.findByText("conv_b"));

    expect(useChatStore.getState().conversationId).toBe("conv_b");
    expect(screen.getByTestId("location")).toHaveTextContent("/c/conv_b");
  });

  it("leaves the store on a modified click that opens a new tab", async () => {
    renderSessions();

    fireEvent.click(await screen.findByText("conv_b"), { metaKey: true });

    expect(useChatStore.getState().conversationId).toBe("conv_a");
    expect(screen.getByTestId("location")).toHaveTextContent("/c/conv_a");
  });

  it("does not call switchTo when clicking the active row", async () => {
    renderSessions([conversation("conv_a")]);
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

    fireEvent.click(await screen.findByText("conv_a"));

    expect(switchTo).not.toHaveBeenCalled();
    expect(useChatStore.getState().conversationId).toBe("conv_a");
  });

  it.each([
    [{ nested_draft_source_id: "conv_source" }, "/?sibling=conv_source"],
    [{ project_draft_name: "Example project" }, "/?project=Example%20project"],
  ] satisfies [Partial<Conversation>, string][])(
    "draft rows navigate without switching first: %o",
    async (draft, destination) => {
      renderSessions([conversation("conv_draft", draft)]);
      const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

      fireEvent.click(await screen.findByText("conv_draft"));

      expect(switchTo).not.toHaveBeenCalled();
      expect(useChatStore.getState().conversationId).toBe("conv_a");
      expect(screen.getByTestId("location")).toHaveTextContent(destination);
    },
  );
});
