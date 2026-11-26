import { afterEach, describe, expect, it, vi } from "vitest";

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

afterEach(() => {
  useChatStore.setState(initialStoreState);
  conversationRegistry.clear();
  clearWarmSwitchInFlight();
  vi.restoreAllMocks();
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
    expect(warmSwitchInFlight("conv_b")).toBe(true);
  });

  it("leaves the store alone when the flag is off", () => {
    useChatStore.setState({ conversationId: "conv_a" });

    switchSessionBeforeNavigate("conv_b");

    expect(useChatStore.getState().conversationId).toBe("conv_a");
    expect(conversationRegistry.peek("conv_b")).toBeUndefined();
    expect(warmSwitchInFlight("conv_b")).toBe(false);
  });

  it("only reports the window for the in-flight target and clears it", () => {
    spyFeatures({ warm_switch_keep_mounted_ui: true });
    useChatStore.setState({ conversationId: "conv_a" });
    const entry = conversationRegistry.acquire("conv_b");
    entry.setState({ abortController: new AbortController(), blocks: [] });

    switchSessionBeforeNavigate("conv_b");

    expect(warmSwitchInFlight("conv_a")).toBe(false);
    expect(warmSwitchInFlight(null)).toBe(false);
    clearWarmSwitchInFlight();
    expect(warmSwitchInFlight("conv_b")).toBe(false);
  });
});
