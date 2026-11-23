import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  steppedSessionId,
  usePublishSidebarSessionOrder,
  useSidebarSessionOrderStore,
} from "./useSidebarSessionOrder";

afterEach(() =>
  useSidebarSessionOrderStore.setState({ orderedSessionIds: [], activeSessionId: null }),
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
    const view = renderHook(() => usePublishSidebarSessionOrder(["a", "b"], "b", true));
    expect(useSidebarSessionOrderStore.getState()).toEqual({
      orderedSessionIds: ["a", "b"],
      activeSessionId: "b",
    });
    view.unmount();
    expect(useSidebarSessionOrderStore.getState()).toEqual({
      orderedSessionIds: [],
      activeSessionId: null,
    });
  });
});

describe("usePublishSidebarSessionOrder, not enabled", () => {
  it("leaves the store alone, as it does once publishing stops", () => {
    useSidebarSessionOrderStore.setState({ orderedSessionIds: ["x"], activeSessionId: "x" });
    const view = renderHook(() => usePublishSidebarSessionOrder(["a", "b"], "b", false));
    expect(useSidebarSessionOrderStore.getState().orderedSessionIds).toEqual(["x"]);
    view.unmount();
    expect(useSidebarSessionOrderStore.getState().orderedSessionIds).toEqual(["x"]);
  });
});
