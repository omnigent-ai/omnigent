import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useWorkspaceHistory } from "./useWorkspaceHistory";
import { resetWorkspaceTabsSessionsForTests, workspaceTabsSession } from "./workspaceTabsSessions";

afterEach(() => {
  cleanup();
  resetWorkspaceTabsSessionsForTests();
  window.localStorage.clear();
});

describe("useWorkspaceHistory", () => {
  it("names where Back lands and steps there", async () => {
    const { store } = workspaceTabsSession("conv_history");
    await act(() => store.open({ place: { kind: "files" } }, { opener: "user" }));
    await act(() =>
      store.open({ place: { kind: "changes" } }, { opener: "user", disposition: "new" }),
    );
    const { result } = renderHook(() => useWorkspaceHistory("conv_history"));
    expect(result.current.facts?.back.target).toBe("Files");
    expect(result.current.facts?.forward.target).toBeNull();

    await act(async () => result.current.go("back"));

    expect(store.selectedTab()?.place.kind).toBe("files");
    expect(result.current.facts?.forward.target).toBe("Diffs");
  });

  it("has nothing to step without a session", () => {
    const { result } = renderHook(() => useWorkspaceHistory(null));
    expect(result.current.facts).toBeNull();
    expect(() => result.current.go("back")).not.toThrow();
  });
});
