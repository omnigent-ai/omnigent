import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as capabilities from "@/lib/capabilities";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useChatStore } from "@/store/chatStore";
import {
  clearWarmSwitchInFlight,
  switchSessionBeforeNavigate,
  warmSwitchInFlight,
  warmSwitchKeepMountedEnabled,
} from "./warmSessionSwitch";

const initialStoreState = useChatStore.getState();

beforeEach(() => {
  vi.useFakeTimers();
  window.history.replaceState({}, "", "/c/conv_a");
  vi.spyOn(capabilities, "getCachedServerInfo").mockReturnValue(null);
});

afterEach(() => {
  useChatStore.setState(initialStoreState);
  conversationRegistry.clear();
  clearWarmSwitchInFlight();
  window.history.replaceState({}, "", "/");
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function spyFeatures(features: Record<string, boolean>) {
  return vi.spyOn(capabilities, "getCachedServerInfo").mockReturnValue({
    ...capabilities.FALLBACK_SERVER_INFO,
    features,
  });
}

describe("warmSwitchKeepMountedEnabled", () => {
  it("is off while the server info is missing or loading", () => {
    expect(warmSwitchKeepMountedEnabled()).toBe(false);
  });

  it("reads the warm_switch_keep_mounted_ui release feature", () => {
    const cached = spyFeatures({ warm_switch_keep_mounted_ui: true });
    expect(warmSwitchKeepMountedEnabled()).toBe(true);
    cached.mockReturnValue({ ...capabilities.FALLBACK_SERVER_INFO, features: {} });
    expect(warmSwitchKeepMountedEnabled()).toBe(false);
  });
});

describe("switchSessionBeforeNavigate", () => {
  it("switches the store to a live target during the click and marks it in flight", () => {
    spyFeatures({ warm_switch_keep_mounted_ui: true });
    useChatStore.setState({ conversationId: "conv_a" });
    // A retained entry with an open stream — what a warm switch paints from.
    const entry = conversationRegistry.acquire("conv_b");
    entry.setState({ abortController: new AbortController(), blocks: [] });

    switchSessionBeforeNavigate("conv_b");

    // The synchronous half of `switchTo` (root write + live mirror) ran before
    // the navigation commit that follows the click.
    expect(useChatStore.getState().conversationId).toBe("conv_b");
    expect(useChatStore.getState().loadingConversation).toBe(false);
    // The store is now one frame ahead of the URL; the gate reads this window.
    expect(warmSwitchInFlight("conv_b", "conv_a")).toBe(true);
  });

  it("leaves the store alone when the flag is off", () => {
    useChatStore.setState({ conversationId: "conv_a" });

    switchSessionBeforeNavigate("conv_b");

    expect(useChatStore.getState().conversationId).toBe("conv_a");
    expect(conversationRegistry.peek("conv_b")).toBeUndefined();
    expect(warmSwitchInFlight("conv_b", "conv_a")).toBe(false);
  });

  it("does not open a window for the already-active session", () => {
    spyFeatures({ warm_switch_keep_mounted_ui: true });
    useChatStore.setState({ conversationId: "conv_a" });
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

    switchSessionBeforeNavigate("conv_a");

    expect(switchTo).not.toHaveBeenCalled();
    expect(warmSwitchInFlight("conv_a", "conv_a")).toBe(false);
    expect(warmSwitchInFlight("conv_a", "conv_b")).toBe(false);
  });

  it("leaves a null source alone so a canceled settings click cannot claim the landing route", () => {
    spyFeatures({ warm_switch_keep_mounted_ui: true });
    useChatStore.setState({ conversationId: null });
    window.history.replaceState({}, "", "/settings");
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

    switchSessionBeforeNavigate("conv_b");
    window.history.pushState({}, "", "/");
    vi.advanceTimersByTime(3000);

    expect(switchTo).not.toHaveBeenCalled();
    expect(useChatStore.getState().conversationId).toBeNull();
    expect(warmSwitchInFlight("conv_b", undefined)).toBe(false);
  });

  it("uses the store source even when the click starts outside a chat route", () => {
    spyFeatures({ warm_switch_keep_mounted_ui: true });
    useChatStore.setState({ conversationId: "conv_a" });
    window.history.replaceState({}, "", "/settings");
    const entry = conversationRegistry.acquire("conv_b");
    entry.setState({ abortController: new AbortController(), blocks: [] });

    switchSessionBeforeNavigate("conv_b");

    expect(warmSwitchInFlight("conv_b", "conv_a")).toBe(true);
    expect(warmSwitchInFlight("conv_b", undefined)).toBe(false);
    window.history.pushState({}, "", "/");
    vi.advanceTimersByTime(3000);
    expect(warmSwitchInFlight("conv_b", "conv_a")).toBe(false);
  });

  it("only reports the window for the in-flight target and source URL and clears it", () => {
    spyFeatures({ warm_switch_keep_mounted_ui: true });
    useChatStore.setState({ conversationId: "conv_a" });
    const entry = conversationRegistry.acquire("conv_b");
    entry.setState({ abortController: new AbortController(), blocks: [] });

    switchSessionBeforeNavigate("conv_b");

    expect(warmSwitchInFlight("conv_a", "conv_a")).toBe(false);
    expect(warmSwitchInFlight("conv_b", "conv_b")).toBe(false);
    expect(warmSwitchInFlight("conv_b", "conv_c")).toBe(false);
    expect(warmSwitchInFlight(null, "conv_a")).toBe(false);
    clearWarmSwitchInFlight();
    expect(warmSwitchInFlight("conv_b", "conv_a")).toBe(false);
  });

  it.each([
    ["/c/conv_a?diff=1", "conv_a"],
    ["/c/conv_b", "conv_b"],
  ])("expires the click window at %s and leaves the store on %s", (pathname, expected) => {
    spyFeatures({ warm_switch_keep_mounted_ui: true });
    for (const id of ["conv_a", "conv_b"]) {
      conversationRegistry.acquire(id).setState({ abortController: new AbortController() });
    }
    useChatStore.setState({ conversationId: "conv_a" });
    switchSessionBeforeNavigate("conv_b");
    window.history.replaceState({}, "", pathname);

    vi.advanceTimersByTime(2999);
    expect(useChatStore.getState().conversationId).toBe("conv_b");
    expect(warmSwitchInFlight("conv_b", "conv_a")).toBe(true);
    vi.advanceTimersByTime(1);

    expect(useChatStore.getState().conversationId).toBe(expected);
    expect(warmSwitchInFlight("conv_b", "conv_a")).toBe(false);
  });

  it("clearing the marker cancels recovery and leaves no timer work", () => {
    spyFeatures({ warm_switch_keep_mounted_ui: true });
    useChatStore.setState({ conversationId: "conv_a" });
    conversationRegistry.acquire("conv_b").setState({ abortController: new AbortController() });
    switchSessionBeforeNavigate("conv_b");
    clearWarmSwitchInFlight();
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

    vi.advanceTimersByTime(3000);

    expect(switchTo).not.toHaveBeenCalled();
    expect(warmSwitchInFlight("conv_b", "conv_a")).toBe(false);
  });

  it("gives a second click its own source and full recovery deadline", () => {
    spyFeatures({ warm_switch_keep_mounted_ui: true });
    for (const id of ["conv_a", "conv_b", "conv_c"]) {
      conversationRegistry.acquire(id).setState({ abortController: new AbortController() });
    }
    useChatStore.setState({ conversationId: "conv_a" });
    switchSessionBeforeNavigate("conv_b");
    vi.advanceTimersByTime(2000);
    switchSessionBeforeNavigate("conv_c");

    vi.advanceTimersByTime(1000);
    expect(useChatStore.getState().conversationId).toBe("conv_c");
    expect(warmSwitchInFlight("conv_c", "conv_b")).toBe(true);
    expect(warmSwitchInFlight("conv_c", "conv_a")).toBe(false);
    vi.advanceTimersByTime(1999);
    expect(useChatStore.getState().conversationId).toBe("conv_c");
    vi.advanceTimersByTime(1);
    expect(useChatStore.getState().conversationId).toBe("conv_b");
    expect(warmSwitchInFlight("conv_c", "conv_b")).toBe(false);
  });
});
