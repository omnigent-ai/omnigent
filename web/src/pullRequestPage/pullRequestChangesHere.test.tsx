import type { ReactNode } from "react";
import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type * as DesktopPullRequestWrites from "@/lib/desktopPullRequestWrites";

const PR = "https://github.com/example-org/example-repo/pull/7";
const ROUTE = vi.hoisted(() => ({
  target: {
    provider: "github" as const,
    host: "github.com",
    repository: "example-org/example-repo",
    number: 7,
    url: "https://github.com/example-org/example-repo/pull/7",
  },
  actor: { login: "editor-login", generation: 1 },
  reader: null,
}));
// This Mac can make the change as the viewer's own account.
vi.mock("@/lib/desktopPullRequestWrites", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopPullRequestWrites>()),
  macWriteRoute: () => ROUTE,
  macWriteDispatch: () => ({ kind: "mac", route: ROUTE }),
}));

import { useMacWriteDispatch, useMacWriteRoute } from "@/newPullRequestPage/hooks/usePullRequests";
import { PullRequestChangesHereContext } from "./pullRequestChangesHere";

function inView(changesHere: boolean) {
  return ({ children }: { children: ReactNode }) => (
    <PullRequestChangesHereContext.Provider value={changesHere}>
      {children}
    </PullRequestChangesHereContext.Provider>
  );
}

describe("a view's changes on this Mac", () => {
  it("go to this Mac where the view may change its pull request", () => {
    const route = renderHook(() => useMacWriteRoute(PR), { wrapper: inView(true) });
    const dispatch = renderHook(() => useMacWriteDispatch(PR), { wrapper: inView(true) });
    expect(route.result.current).toBe(ROUTE);
    expect(dispatch.result.current).toEqual({ kind: "mac", route: ROUTE });
  });

  it("never go to this Mac from a view that may not, as any account", () => {
    const route = renderHook(() => useMacWriteRoute(PR), { wrapper: inView(false) });
    const dispatch = renderHook(() => useMacWriteDispatch(PR), { wrapper: inView(false) });
    expect(route.result.current).toBeNull();
    expect(dispatch.result.current).toEqual({ kind: "host" });
  });

  it("go where their route allows outside any pull request view", () => {
    const { result } = renderHook(() => useMacWriteRoute(PR));
    expect(result.current).toBe(ROUTE);
  });
});
