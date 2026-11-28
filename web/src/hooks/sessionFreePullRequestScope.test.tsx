import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { expect, it, vi } from "vitest";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(() => true),
  useSessionHostOnline: vi.fn(() => true),
}));

import { SessionFreePullRequestScopeContext } from "./sessionFreePullRequestScope";
import { usePullRequestViewServeable } from "./usePullRequestViewServeable";

const SESSION_FREE_SCOPE = { id: "pull-requests-page" };

it("asks no host in a session-free pull request view, though the runner is online", () => {
  const scoped = renderHook(() => usePullRequestViewServeable("pull-requests-page"), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <SessionFreePullRequestScopeContext.Provider value={SESSION_FREE_SCOPE}>
        {children}
      </SessionFreePullRequestScopeContext.Provider>
    ),
  });
  expect(scoped.result.current).toBe(false);
  expect(renderHook(() => usePullRequestViewServeable("conv-1")).result.current).toBe(true);
});
