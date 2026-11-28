import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { basenamedRouting, reactRouterRouting } from "@/lib/routing";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useChatStore } from "@/store/chatStore";
import {
  isWarmSwitchPending,
  switchSessionBeforeNavigate,
  useWarmSwitchConversationId,
} from "./warmSessionSwitch";

const initialStoreState = useChatStore.getState();
const rebasePath = reactRouterRouting.rebasePath;

beforeEach(() => {
  vi.useFakeTimers();
  window.history.replaceState({}, "", "/c/conv_a");
});

afterEach(() => {
  cleanup();
  vi.clearAllTimers();
  useChatStore.setState(initialStoreState);
  conversationRegistry.clear();
  window.history.replaceState({}, "", "/");
  delete window.__OMNIGENT_BASE_PATH__;
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function seedLive(...ids: string[]) {
  for (const id of ids) {
    conversationRegistry.acquire(id).setState({ abortController: new AbortController() });
  }
}

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
      window.__OMNIGENT_BASE_PATH__ = base;
      window.history.replaceState({}, "", pathname);
      expect(isWarmSwitchPending(active, url, rebasePath)).toBe(expected);
    },
  );

  it.each([
    ["/ml/omnigent-embed/c/conv_b", true],
    ["/ml/omnigent-embed/canvas/c/conv_b", false],
    ["/c/conv_b", false],
    ["/ml/omnigent-embed-other/c/conv_b", false],
  ] as const)("matches the active embed route at %s: %s", (pathname, expected) => {
    window.history.replaceState({}, "", pathname);
    expect(
      isWarmSwitchPending("conv_b", "conv_a", basenamedRouting("/ml/omnigent-embed").rebasePath),
    ).toBe(expected);
  });
});

describe("useWarmSwitchConversationId", () => {
  it("tracks the store only during a browser-first navigation", () => {
    useChatStore.setState({ conversationId: "conv_a" });
    const { result, rerender } = renderHook(({ id }) => useWarmSwitchConversationId(id), {
      initialProps: { id: "conv_a" },
    });
    act(() => {
      window.history.pushState({}, "", "/c/conv_b");
      useChatStore.setState({ conversationId: "conv_b" });
    });
    expect(result.current).toBe("conv_b");
    rerender({ id: "conv_b" });
    expect(result.current).toBe("conv_b");
  });
});

describe("switchSessionBeforeNavigate", () => {
  it("switches a live target synchronously and leaves successful navigation alone", () => {
    seedLive("conv_a", "conv_b");
    useChatStore.setState({ conversationId: "conv_a" });
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

    switchSessionBeforeNavigate("conv_b", rebasePath);
    expect(useChatStore.getState().conversationId).toBe("conv_b");
    expect(useChatStore.getState().loadingConversation).toBe(false);
    window.history.pushState({}, "", "/c/conv_b");
    vi.runOnlyPendingTimers();

    expect(switchTo.mock.calls).toEqual([["conv_b"]]);
    expect(useChatStore.getState().conversationId).toBe("conv_b");
  });

  it.each([
    { targets: ["conv_b"], basename: "" },
    { targets: ["conv_b", "conv_c"], basename: "" },
    { targets: ["conv_b", "conv_c"], basename: "/ml/omnigent-embed" },
  ])(
    "restores canceled clicks to the browser source on the next task: $targets under $basename",
    ({ targets, basename }) => {
      seedLive("conv_a", "conv_b", "conv_c");
      useChatStore.setState({ conversationId: "conv_a" });
      const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");
      const clickRebasePath = basename ? basenamedRouting(basename).rebasePath : rebasePath;
      window.history.replaceState({}, "", clickRebasePath("/c/conv_a"));

      for (const target of targets) switchSessionBeforeNavigate(target, clickRebasePath);
      window.history.replaceState({}, "", clickRebasePath("/c/conv_a?diff=1"));
      expect(useChatStore.getState().conversationId).toBe(targets.at(-1));
      vi.runOnlyPendingTimers();

      expect(switchTo.mock.calls).toEqual([...targets.map((target) => [target]), ["conv_a"]]);
      expect(useChatStore.getState().conversationId).toBe("conv_a");
    },
  );

  it("does not let a canceled click overwrite a newer store switch", () => {
    seedLive("conv_a", "conv_b", "conv_c");
    useChatStore.setState({ conversationId: "conv_a" });
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

    switchSessionBeforeNavigate("conv_b", rebasePath);
    void useChatStore.getState().switchTo("conv_c");
    vi.runOnlyPendingTimers();

    expect(switchTo.mock.calls).toEqual([["conv_b"], ["conv_c"]]);
    expect(useChatStore.getState().conversationId).toBe("conv_c");
  });

  it("skips the fast path while the browser still identifies a temporary source", () => {
    seedLive("conv_a", "conv_b");
    useChatStore.setState({ conversationId: "conv_a" });
    window.history.replaceState({}, "", "/c/temp:source");
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

    switchSessionBeforeNavigate("conv_b", rebasePath);
    vi.runOnlyPendingTimers();

    expect(switchTo).not.toHaveBeenCalled();
    expect(useChatStore.getState().conversationId).toBe("conv_a");
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
      if (stream !== "missing") {
        conversationRegistry.acquire(target).setState({
          abortController: stream === "stopped" ? null : new AbortController(),
          conversationLoadError: stream === "failed" ? new Error("load failed") : null,
        });
      }
      useChatStore.setState({ conversationId: source });
      const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

      switchSessionBeforeNavigate(target, rebasePath);
      vi.runOnlyPendingTimers();

      expect(switchTo).not.toHaveBeenCalled();
      expect(useChatStore.getState().conversationId).toBe(source);
    },
  );
});
