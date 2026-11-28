// One refresh at a time, and a failure kept until a later refresh of the same pull request goes
// through; another pull request starts clean.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const attempts = vi.hoisted(() => [] as { prUrl: string; resolve: (result: unknown) => void }[]);
vi.mock("./refreshPullRequest", () => ({
  refreshPullRequest: (_client: unknown, target: { prUrl: string }) =>
    new Promise((resolve) => {
      attempts.push({ prUrl: target.prUrl, resolve });
    }),
}));

import { usePullRequestRefresh } from "./usePullRequestRefresh";

const PR = "https://github.com/acme/app/pull/7";
const OTHER = "https://github.com/acme/app/pull/8";

function renderRefresh(prUrl: string) {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(({ url }) => usePullRequestRefresh("conv_1", url), {
    wrapper,
    initialProps: { url: prUrl },
  });
}

describe("usePullRequestRefresh", () => {
  it("runs one refresh at a time, and keeps a failure until one goes through", async () => {
    attempts.length = 0;
    const { result } = renderRefresh(PR);
    act(() => result.current.refresh());
    act(() => result.current.refresh());
    expect(attempts).toHaveLength(1);
    expect(result.current.refreshing).toBe(true);

    await act(async () => attempts[0].resolve({ kind: "failed", error: new Error("No answer.") }));
    expect(result.current).toMatchObject({
      refreshing: false,
      failure: { kind: "failed", message: "No answer." },
    });

    // Trying again keeps the failure showing until it goes through.
    act(() => result.current.refresh());
    expect(result.current.failure).not.toBeNull();
    await act(async () => attempts[1].resolve({ kind: "timed-out" }));
    expect(result.current.failure).toEqual({ kind: "timed-out" });
    act(() => result.current.refresh());
    await act(async () => attempts[2].resolve({ kind: "ok" }));
    expect(result.current).toMatchObject({ refreshing: false, failure: null });
  });

  it("keeps each pull request's refresh apart, so an earlier one never settles a later one", async () => {
    attempts.length = 0;
    const { result, rerender } = renderRefresh(PR);
    act(() => result.current.refresh());
    rerender({ url: OTHER });
    expect(result.current).toMatchObject({ refreshing: false, failure: null });
    act(() => result.current.refresh());
    rerender({ url: PR });
    // Still running for A: asking again waits for it.
    expect(result.current.refreshing).toBe(true);
    act(() => result.current.refresh());
    expect(attempts.map((attempt) => attempt.prUrl)).toEqual([PR, OTHER]);
    // B's answer is B's alone.
    await act(async () => attempts[1].resolve({ kind: "failed", error: new Error("B failed.") }));
    expect(result.current).toMatchObject({ refreshing: true, failure: null });
    await act(async () => attempts[0].resolve({ kind: "ok" }));
    expect(result.current).toMatchObject({ refreshing: false, failure: null });
    // A new attempt for A after that one ended starts on its own.
    act(() => result.current.refresh());
    expect(attempts).toHaveLength(3);
    rerender({ url: OTHER });
    expect(result.current.failure).toEqual({ kind: "failed", message: "B failed." });
  });

  it("keeps the same pull request in another session apart", async () => {
    attempts.length = 0;
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result, rerender } = renderHook(({ session }) => usePullRequestRefresh(session, PR), {
      wrapper,
      initialProps: { session: "conv_1" },
    });
    act(() => result.current.refresh());
    rerender({ session: "conv_2" });
    expect(result.current.refreshing).toBe(false);
    act(() => result.current.refresh());
    expect(attempts).toHaveLength(2);
    await act(async () =>
      attempts[0].resolve({ kind: "failed", error: new Error("conv_1 failed.") }),
    );
    expect(result.current).toMatchObject({ refreshing: true, failure: null });
  });
});
