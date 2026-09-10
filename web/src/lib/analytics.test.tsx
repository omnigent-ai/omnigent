import { afterEach, describe, expect, it, vi } from "vitest";
import { render, renderHook, act } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import type { ReactNode } from "react";

import { setOmnigentHostConfig, type OmnigentAnalyticsEvent } from "@/lib/host";
import {
  emitOmnigentAnalytics,
  emitInteractionPhase,
  useOmnigentAnalytics,
  useOmnigentPageView,
} from "@/lib/analytics";
import { startTimedInteraction } from "@/lib/analyticsEmit";

afterEach(() => {
  // Reset the module-singleton host config between tests. The empty-config
  // guard only blocks clobbering a fetcher, so an analytics-only reset clears.
  setOmnigentHostConfig({});
});

describe("emitOmnigentAnalytics", () => {
  it("is a no-op when no host sink is configured", () => {
    // No throw, nothing to observe — standalone behavior.
    expect(() => emitOmnigentAnalytics({ type: "click", componentId: "x" })).not.toThrow();
  });

  it("forwards the event to the host sink when configured", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    const event: OmnigentAnalyticsEvent = {
      type: "click",
      componentId: "x",
      componentKind: "button",
    };
    emitOmnigentAnalytics(event);
    expect(analytics).toHaveBeenCalledExactlyOnceWith(event);
  });

  it("swallows a throwing host sink so it can't break the primary action", () => {
    // The wrappers emit BEFORE running the caller's handler, so a sink that
    // throws must not propagate up and suppress the user's action.
    setOmnigentHostConfig({
      analytics: () => {
        throw new Error("host sink blew up");
      },
    });
    expect(() => emitOmnigentAnalytics({ type: "click", componentId: "x" })).not.toThrow();
  });
});

describe("emitInteractionPhase", () => {
  it("is a no-op when no host sink is configured", () => {
    expect(() =>
      emitInteractionPhase({
        interactionId: "run_1",
        interactionKind: "agent_run",
        phase: "start",
      }),
    ).not.toThrow();
  });

  it("forwards a start phase, then a complete phase carrying status and duration", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });

    emitInteractionPhase({ interactionId: "run_1", interactionKind: "agent_run", phase: "start" });
    expect(analytics).toHaveBeenLastCalledWith({
      type: "interaction_phase",
      interactionId: "run_1",
      interactionKind: "agent_run",
      phase: "start",
    });

    emitInteractionPhase({
      interactionId: "run_1",
      interactionKind: "agent_run",
      phase: "complete",
      status: "success",
      durationMs: 1234,
    });
    expect(analytics).toHaveBeenLastCalledWith({
      type: "interaction_phase",
      interactionId: "run_1",
      interactionKind: "agent_run",
      phase: "complete",
      status: "success",
      durationMs: 1234,
    });
  });
});

describe("startTimedInteraction", () => {
  it("emits start immediately, then complete(success) with duration on complete()", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });

    const interaction = startTimedInteraction("get_session", "sess_1");
    expect(analytics).toHaveBeenCalledExactlyOnceWith({
      type: "interaction_phase",
      interactionId: "sess_1",
      interactionKind: "get_session",
      phase: "start",
    });

    interaction.complete();
    expect(analytics).toHaveBeenLastCalledWith({
      type: "interaction_phase",
      interactionId: "sess_1",
      interactionKind: "get_session",
      phase: "complete",
      status: "success",
      durationMs: expect.any(Number),
    });
  });

  it("fail() completes with a failure status", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });

    startTimedInteraction("create_session_sandbox", "sess_2").fail();

    expect(analytics).toHaveBeenLastCalledWith({
      type: "interaction_phase",
      interactionId: "sess_2",
      interactionKind: "create_session_sandbox",
      phase: "complete",
      status: "failure",
      durationMs: expect.any(Number),
    });
  });

  it("is idempotent — only the first settle emits a complete", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });

    const interaction = startTimedInteraction("list_sessions", "l1");
    interaction.complete();
    interaction.fail(); // ignored
    interaction.complete(); // ignored

    // Exactly one start + one complete.
    expect(analytics).toHaveBeenCalledTimes(2);
    expect(analytics).toHaveBeenLastCalledWith(
      expect.objectContaining({ phase: "complete", status: "success" }),
    );
  });

  it("generates one correlation id shared by start and complete when none is given", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });

    startTimedInteraction("list_sessions").complete();

    const phases = analytics.mock.calls.map(
      (c) => c[0] as { interactionId: string; phase: string },
    );
    expect(phases[0]?.interactionId).toBe(phases[1]?.interactionId);
    expect(phases[0]?.interactionId).toEqual(expect.any(String));
  });

  it("never throws even when the host sink throws (telemetry can't break the caller)", () => {
    setOmnigentHostConfig({
      analytics: () => {
        throw new Error("host sink blew up");
      },
    });

    expect(() => startTimedInteraction("get_session", "s").complete()).not.toThrow();
  });
});

describe("useOmnigentAnalytics", () => {
  it("forwards trackInteraction to the host sink", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    const { result } = renderHook(() => useOmnigentAnalytics());

    result.current.trackInteraction({
      interactionId: "call_1",
      interactionKind: "tool_call",
      phase: "complete",
      name: "shell",
      durationMs: 42,
    });
    expect(analytics).toHaveBeenCalledExactlyOnceWith({
      type: "interaction_phase",
      interactionId: "call_1",
      interactionKind: "tool_call",
      phase: "complete",
      name: "shell",
      durationMs: 42,
    });
  });

  it("redacts field values by default and forwards only when declared PII-free", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    const { result } = renderHook(() => useOmnigentAnalytics());

    result.current.trackValueChange("field", "input", "secret text");
    expect(analytics).toHaveBeenLastCalledWith({
      type: "value_change",
      componentId: "field",
      componentKind: "input",
      value: undefined,
    });

    result.current.trackValueChange("filter", "select", "active", { valueHasNoPii: true });
    expect(analytics).toHaveBeenLastCalledWith({
      type: "value_change",
      componentId: "filter",
      componentKind: "select",
      value: "active",
    });
  });
});

describe("useOmnigentPageView", () => {
  // useOmnigentPageView reads useLocation, so it needs a router. Render at a
  // fixed path and let a rerender's `path` prop drive navigation.
  const atPath = (path: string) => {
    const Wrapper = ({ children }: { children: ReactNode }) => (
      <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
    );
    return Wrapper;
  };

  it("fires exactly one page_view on mount", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    renderHook(() => useOmnigentPageView("chat"), { wrapper: atPath("/c/a") });
    expect(analytics).toHaveBeenCalledExactlyOnceWith({ type: "page_view", pageId: "chat" });
  });

  it("does not re-fire on re-render at the same path", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    const { rerender } = renderHook(() => useOmnigentPageView("chat"), { wrapper: atPath("/c/a") });
    rerender();
    expect(analytics).toHaveBeenCalledTimes(1);
  });

  it("re-fires under the same pageId when the pathname changes (chat /c/a -> /c/b)", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    // One mounted component (like ChatPage) that navigates between conversations
    // in place. The hook must re-emit on the pathname change even though pageId
    // stays "chat" — the regression this guards against.
    let navigate: ReturnType<typeof useNavigate>;
    function ChatLike() {
      useOmnigentPageView("chat");
      navigate = useNavigate();
      return null;
    }
    render(
      <MemoryRouter initialEntries={["/c/a"]}>
        <ChatLike />
      </MemoryRouter>,
    );
    expect(analytics).toHaveBeenCalledTimes(1);
    act(() => navigate("/c/b"));
    expect(analytics).toHaveBeenCalledTimes(2);
    expect(analytics).toHaveBeenLastCalledWith({ type: "page_view", pageId: "chat" });
  });
});
