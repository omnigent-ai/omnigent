import { act, render, renderHook } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  currentSidebarSessionId,
  PENDING_SESSION_MS,
  stepSidebarSession,
  steppedSessionId,
  usePublishSidebarSessionOrder,
  useSidebarSessionOrderStore,
} from "./useSidebarSessionOrder";

afterEach(() =>
  useSidebarSessionOrderStore.setState({
    orderedSessionIds: [],
    activeSessionId: null,
    pendingSessionId: null,
  }),
);

describe("steppedSessionId", () => {
  const rows = ["a", "b", "c"];

  it("steps down and up from the open row, wrapping at the ends", () => {
    expect(steppedSessionId(rows, "a", 1)).toBe("b");
    expect(steppedSessionId(rows, "c", 1)).toBe("a");
    expect(steppedSessionId(rows, "a", -1)).toBe("c");
  });

  it("enters at the top going down and at the bottom going up from off the list", () => {
    expect(steppedSessionId(rows, null, 1)).toBe("a");
    expect(steppedSessionId(rows, "elsewhere", -1)).toBe("c");
  });

  it("has nowhere to go with nothing listed", () => {
    expect(steppedSessionId([], "a", 1)).toBeUndefined();
  });
});

describe("usePublishSidebarSessionOrder", () => {
  it("publishes the sidebar's rows while mounted and clears them once it unmounts", () => {
    const view = renderHook(() => usePublishSidebarSessionOrder(["a", "b"], "b"), {
      wrapper: MemoryRouter,
    });
    expect(useSidebarSessionOrderStore.getState()).toEqual({
      published: true,
      orderedSessionIds: ["a", "b"],
      activeSessionId: "b",
      pendingSessionId: null,
      pendingSince: 0,
    });
    view.unmount();
    expect(useSidebarSessionOrderStore.getState()).toEqual({
      published: false,
      orderedSessionIds: [],
      activeSessionId: null,
      pendingSessionId: null,
      pendingSince: 0,
    });
  });
});

describe("stepSidebarSession", () => {
  it("steps again from the session it opened before the route renders it", () => {
    useSidebarSessionOrderStore.setState({
      orderedSessionIds: ["a", "b", "c"],
      activeSessionId: "a",
    });
    const navigate = vi.fn();
    expect(stepSidebarSession(1, navigate)).toBe(true);
    expect(stepSidebarSession(1, navigate)).toBe(true);
    expect(stepSidebarSession(-1, navigate)).toBe(true);
    expect(navigate.mock.calls).toEqual([["/c/b"], ["/c/c"], ["/c/b"]]);
  });

  it("starts from the rendered session again once a navigation commits", () => {
    const navigate = vi.fn();
    function Publisher() {
      usePublishSidebarSessionOrder(["a", "b", "c"], "a");
      const go = useNavigate();
      return (
        <button type="button" onClick={() => go("/elsewhere")}>
          go
        </button>
      );
    }
    const view = render(
      <MemoryRouter>
        <Publisher />
      </MemoryRouter>,
    );
    stepSidebarSession(1, navigate);
    expect(currentSidebarSessionId()).toBe("b");
    act(() => view.getByRole("button").click());
    expect(currentSidebarSessionId()).toBe("a");
  });

  it("forgets a step whose navigation never commits, so steps and a retry start over", () => {
    vi.useFakeTimers();
    try {
      useSidebarSessionOrderStore.setState({
        orderedSessionIds: ["a", "b", "c"],
        activeSessionId: "a",
      });
      // A guard, such as unsaved edits, cancels the navigation to b.
      const navigate = vi.fn();
      stepSidebarSession(1, navigate);
      expect(currentSidebarSessionId()).toBe("b");
      vi.advanceTimersByTime(PENDING_SESSION_MS);
      expect(currentSidebarSessionId()).toBe("a");
      stepSidebarSession(1, navigate);
      expect(navigate.mock.calls).toEqual([["/c/b"], ["/c/b"]]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("has nowhere to go with nothing listed", () => {
    const navigate = vi.fn();
    expect(stepSidebarSession(1, navigate)).toBe(false);
    expect(navigate).not.toHaveBeenCalled();
  });
});
