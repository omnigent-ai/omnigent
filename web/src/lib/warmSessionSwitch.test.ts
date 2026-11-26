import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as capabilities from "@/lib/capabilities";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useChatStore } from "@/store/chatStore";
import {
  isWarmSwitchPending,
  switchSessionBeforeNavigate,
  warmSwitchKeepMountedEnabled,
} from "./warmSessionSwitch";

const initialStoreState = useChatStore.getState();

beforeEach(() => {
  vi.useFakeTimers();
  window.history.replaceState({}, "", "/c/conv_a");
  vi.spyOn(capabilities, "getCachedServerInfo").mockReturnValue(null);
});

afterEach(() => {
  vi.clearAllTimers();
  useChatStore.setState(initialStoreState);
  conversationRegistry.clear();
  window.history.replaceState({}, "", "/");
  delete window.__OMNIGENT_BASE_PATH__;
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function spyFeatures(features: Record<string, boolean>) {
  return vi.spyOn(capabilities, "getCachedServerInfo").mockReturnValue({
    ...capabilities.FALLBACK_SERVER_INFO,
    features,
  });
}

function seedLive(...ids: string[]) {
  for (const id of ids) {
    conversationRegistry.acquire(id).setState({ abortController: new AbortController() });
  }
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

describe("isWarmSwitchPending", () => {
  it.each([
    ["conv_b", "conv_a", "/c/conv_b", "", true],
    ["conv_b", "conv_b", "/c/conv_b", "", false],
    [null, "conv_a", "/c/conv_b", "", false],
    ["conv_a", "conv_b", "/c/conv_b", "", false],
    ["conv_b", "conv_a", "/c/conv_a", "", false],
    ["conv_b", "conv_a", "/canvas/c/conv_b", "", false],
    ["conv_b", "conv_a", "/c/conv_b/child", "", false],
    ["conv_b", "conv_a", "/proxy/example/c/conv_b", "/proxy/example", true],
    ["conv_b", "conv_a", "/proxy/example/c/conv_b/", "/proxy/example", true],
    ["conv_b", "conv_a", "/c/conv_b", "/proxy/example", false],
    ["conv_b", "conv_a", "/proxy/example/canvas/c/conv_b", "/proxy/example", false],
    ["conv_b", "conv_a", "/proxy/example-other/c/conv_b", "/proxy/example", false],
  ] as const)(
    "store %s, router %s, browser %s, base %s => %s",
    (active, url, pathname, base, expected) => {
      spyFeatures({ warm_switch_keep_mounted_ui: true });
      window.__OMNIGENT_BASE_PATH__ = base;
      window.history.replaceState({}, "", pathname);
      expect(isWarmSwitchPending(active, url)).toBe(expected);
    },
  );

  it("does not retain the mismatch when the flag is off", () => {
    window.history.replaceState({}, "", "/c/conv_b");
    expect(isWarmSwitchPending("conv_b", "conv_a")).toBe(false);
  });
});

describe("switchSessionBeforeNavigate", () => {
  it("switches a live target synchronously and leaves successful navigation alone", () => {
    spyFeatures({ warm_switch_keep_mounted_ui: true });
    seedLive("conv_a", "conv_b");
    useChatStore.setState({ conversationId: "conv_a" });
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

    switchSessionBeforeNavigate("conv_b");
    expect(useChatStore.getState().conversationId).toBe("conv_b");
    expect(useChatStore.getState().loadingConversation).toBe(false);
    window.history.pushState({}, "", "/c/conv_b");
    vi.runOnlyPendingTimers();

    expect(switchTo.mock.calls).toEqual([["conv_b"]]);
    expect(useChatStore.getState().conversationId).toBe("conv_b");
  });

  it("restores a canceled click on the next task even if search parameters changed", () => {
    spyFeatures({ warm_switch_keep_mounted_ui: true });
    seedLive("conv_a", "conv_b");
    useChatStore.setState({ conversationId: "conv_a" });
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

    switchSessionBeforeNavigate("conv_b");
    window.history.replaceState({}, "", "/c/conv_a?diff=1");
    expect(useChatStore.getState().conversationId).toBe("conv_b");
    vi.runOnlyPendingTimers();

    expect(switchTo.mock.calls).toEqual([["conv_b"], ["conv_a"]]);
    expect(useChatStore.getState().conversationId).toBe("conv_a");
  });

  it("leaves the store alone when the flag is off", () => {
    useChatStore.setState({ conversationId: "conv_a" });
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");
    switchSessionBeforeNavigate("conv_b");
    expect(switchTo).not.toHaveBeenCalled();
    expect(conversationRegistry.peek("conv_b")).toBeUndefined();
  });

  it.each([
    { source: null, target: "conv_b", stream: "live" },
    { source: "temp:source", target: "conv_b", stream: "live" },
    { source: "conv_a", target: "temp:target", stream: "live" },
    { source: "conv_a", target: "conv_a", stream: "live" },
    { source: "conv_a", target: "conv_b", stream: "missing" },
    { source: "conv_a", target: "conv_b", stream: "stopped" },
    { source: "conv_a", target: "conv_b", stream: "failed" },
  ])(
    "does not switch first for source $source, target $target, stream $stream",
    ({ source, target, stream }) => {
      spyFeatures({ warm_switch_keep_mounted_ui: true });
      if (stream !== "missing") {
        conversationRegistry.acquire(target).setState({
          abortController: stream === "stopped" ? null : new AbortController(),
          conversationLoadError: stream === "failed" ? new Error("load failed") : null,
        });
      }
      useChatStore.setState({ conversationId: source });
      const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

      switchSessionBeforeNavigate(target);
      vi.runOnlyPendingTimers();

      expect(switchTo).not.toHaveBeenCalled();
      expect(useChatStore.getState().conversationId).toBe(source);
    },
  );
});
