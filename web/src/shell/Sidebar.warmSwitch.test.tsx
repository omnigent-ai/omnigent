// The sidebar row's switch-first wiring: with `warm_switch_keep_mounted_ui` on,
// a plain click on a session row switches the chat store as it navigates; a
// modified click (new tab) and the flag-off path never touch the store.
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as capabilities from "@/lib/capabilities";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { sessionRowsPage } from "@/lib/sidebarData";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useChatStore } from "@/store/chatStore";
import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversation } from "@/test/sidebarMockHelpers";
import { clearWarmSwitchInFlight, warmSwitchInFlight } from "@/lib/warmSessionSwitch";

const initialStoreState = useChatStore.getState();
const fetchMock = vi.fn();

function LocationProbe() {
  const location = useLocation();
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
  clearWarmSwitchInFlight();
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
        : sessionRowsPage([conversation("conv_a"), conversation("conv_b")]);
      return Promise.resolve({ ok: true, json: async () => body });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSidebar({
      route: "/c/conv_a",
      features: { warm_switch_keep_mounted_ui: true },
      children: <LocationProbe />,
    });
    await screen.findByText("conv_b");

    act(() => fireEvent.click(screen.getByText("conv_b")));

    expect(useChatStore.getState().conversationId).toBe("conv_b");
    expect(screen.getByTestId("location")).toHaveTextContent("/c/conv_b");
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
        : sessionRowsPage([conversation("conv_a"), conversation("conv_b")]);
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
        : sessionRowsPage([conversation("conv_a"), conversation("conv_b")]);
      return Promise.resolve({ ok: true, json: async () => body });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSidebar({ route: "/c/conv_a" });
    await screen.findByText("conv_b");

    act(() => fireEvent.click(screen.getByText("conv_b")));

    expect(useChatStore.getState().conversationId).toBe("conv_a");
  });

  it("does not open a switch window when clicking the active row", async () => {
    vi.spyOn(capabilities, "getCachedServerInfo").mockReturnValue({
      ...FALLBACK_SERVER_INFO,
      features: { warm_switch_keep_mounted_ui: true },
    });
    useChatStore.setState({ conversationId: "conv_a" });
    seedLive("conv_a");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => ({
        ok: true,
        json: async () =>
          String(url).includes("/projects") ? [] : sessionRowsPage([conversation("conv_a")]),
      })),
    );
    renderSidebar({ route: "/c/conv_a", features: { warm_switch_keep_mounted_ui: true } });

    fireEvent.click(await screen.findByText("conv_a"));

    expect(warmSwitchInFlight("conv_a", null)).toBe(false);
    expect(useChatStore.getState().conversationId).toBe("conv_a");
  });

  it.each([
    [{ nested_draft_source_id: "conv_source" }, "/?sibling=conv_source"],
    [{ project_draft_name: "Example project" }, "/?project=Example%20project"],
  ])("draft rows navigate without switching first: %o", async (draft, destination) => {
    vi.spyOn(capabilities, "getCachedServerInfo").mockReturnValue({
      ...FALLBACK_SERVER_INFO,
      features: { warm_switch_keep_mounted_ui: true },
    });
    useChatStore.setState({ conversationId: "conv_a" });
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => ({
        ok: true,
        json: async () =>
          String(url).includes("/projects")
            ? []
            : sessionRowsPage([conversation("conv_draft", draft)]),
      })),
    );
    renderSidebar({
      route: "/c/conv_a",
      features: { warm_switch_keep_mounted_ui: true },
      children: <LocationProbe />,
    });

    fireEvent.click(await screen.findByText("conv_draft"));

    expect(switchTo).not.toHaveBeenCalled();
    expect(useChatStore.getState().conversationId).toBe("conv_a");
    expect(screen.getByTestId("location")).toHaveTextContent(destination);
  });
});
