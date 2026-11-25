import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { FakeShell } from "./__fixtures__/browserTabsStore";
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

  it("keeps browser tabs' owners attached while mounted", async () => {
    const shell = new FakeShell("replay");
    const { store } = workspaceTabsSession("conv_history");
    store.openBrowserView("w", { select: true, opener: "user" });
    await act(() =>
      store.open({ place: { kind: "files" } }, { opener: "user", disposition: "new" }),
    );
    const { result, unmount } = renderHook(() => useWorkspaceHistory("conv_history", () => shell));
    expect(shell.listenerCount()).toBe(1);
    expect(result.current.facts?.back.inexactPage).toBe(false);
    unmount();
    expect(shell.listenerCount()).toBe(0);
  });

  it("has nothing to step without a session", () => {
    const { result } = renderHook(() => useWorkspaceHistory(null));
    expect(result.current.facts).toBeNull();
    expect(() => result.current.go("back")).not.toThrow();
  });
});
