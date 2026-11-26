import { renderHook } from "@testing-library/react";
import { type ContextType, createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { FileViewerContext, useShowPullRequests } from "./FileViewerContext";

/** A shell, such as a grid pane, that can only open Classic's Pull Requests tab. */
function classicOnly(openGithubTab: (prUrl?: string) => void) {
  const value = { openGithubTab } as unknown as NonNullable<ContextType<typeof FileViewerContext>>;
  return ({ children }: { children: ReactNode }) =>
    createElement(FileViewerContext.Provider, { value }, children);
}

describe("useShowPullRequests", () => {
  const PR = "https://github.com/acme/app/pull/42";

  it("is null outside a shell", () => {
    expect(renderHook(() => useShowPullRequests()).result.current).toBeNull();
  });

  it("hands the target and options to a shell that shows pull requests itself", () => {
    const openGithubTab = vi.fn();
    const showPullRequests = vi.fn();
    const value = { openGithubTab, showPullRequests } as unknown as NonNullable<
      ContextType<typeof FileViewerContext>
    >;
    const { result } = renderHook(() => useShowPullRequests(), {
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(FileViewerContext.Provider, { value }, children),
    });
    result.current!({ kind: "pull", url: PR }, { classic: "open" });
    expect(showPullRequests).toHaveBeenCalledWith({ kind: "pull", url: PR }, { classic: "open" });
    expect(openGithubTab).not.toHaveBeenCalled();
  });
});

describe("useShowPullRequests without a shell that shows them itself", () => {
  const PR = "https://github.com/acme/app/pull/42";

  it("opens Classic's tab on the pull request asked for", () => {
    const openGithubTab = vi.fn();
    const { result } = renderHook(() => useShowPullRequests(), {
      wrapper: classicOnly(openGithubTab),
    });
    result.current!({ kind: "pull", url: PR }, { classic: "open" });
    expect(openGithubTab).toHaveBeenCalledWith(PR);
  });

  it("opens Classic's tab on its list for the overview, and stays put when asked to", () => {
    const openGithubTab = vi.fn();
    const { result } = renderHook(() => useShowPullRequests(), {
      wrapper: classicOnly(openGithubTab),
    });
    result.current!({ kind: "overview" }, { classic: "open" });
    expect(openGithubTab).toHaveBeenCalledWith(undefined);
    openGithubTab.mockClear();
    result.current!({ kind: "pull", url: PR }, { classic: "stay" });
    expect(openGithubTab).not.toHaveBeenCalled();
  });
});
