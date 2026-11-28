import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  enabledWorkspaceRoutes: vi.fn(),
  servingRoutes: vi.fn(),
}));
vi.mock("@/lib/workspaceApi", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceApiModule>()),
  ...api,
}));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({ features: {} }) }));

import type * as WorkspaceApiModule from "@/lib/workspaceApi";
import { workspaceMethodName, type Route } from "@/lib/workspaceApi";
import {
  pullRequestPageDecision,
  PULL_REQUEST_PAGE_METHODS,
  usePullRequestMethodServed,
  usePullRequestPageRoute,
} from "./pullRequestPageRoute";

const PAGE_METHODS = [
  "GetPullRequest",
  "ListPullRequestFiles",
  "GetPullRequestDiff",
  "ListPullRequestCommits",
  "ListReviewThreads",
  "ListReviewThreadComments",
];

const serving = (route: Route, served: string[], key = `${route}:conv_1`) => ({
  route,
  key,
  served: new Set(served),
});

function renderRoute(sessionId = "conv_1") {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(({ id }) => usePullRequestPageRoute(id), {
    wrapper,
    initialProps: { id: sessionId },
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  api.enabledWorkspaceRoutes.mockReturnValue(["server"]);
});

describe("pullRequestPageDecision", () => {
  it("names the page's reads by the names GetCapabilities lists", () => {
    expect(PULL_REQUEST_PAGE_METHODS.map(workspaceMethodName)).toEqual(PAGE_METHODS);
  });

  it("picks a server route that serves every read the page makes", () => {
    expect(
      pullRequestPageDecision({ serving: [serving("server", PAGE_METHODS)], failures: [] }),
    ).toMatchObject({ route: { route: "server", key: "server:conv_1" }, answered: true });
  });

  it("prefers the direct route when both serve the page", () => {
    const decision = pullRequestPageDecision({
      serving: [serving("direct", PAGE_METHODS), serving("server", PAGE_METHODS)],
      failures: [],
    });
    expect(decision.route?.route).toBe("direct");
  });

  it("takes the server when the direct route's host lacks a read", () => {
    const decision = pullRequestPageDecision({
      serving: [serving("direct", PAGE_METHODS.slice(1)), serving("server", PAGE_METHODS)],
      failures: [],
    });
    expect(decision.route?.route).toBe("server");
  });

  it("leaves a host serving only some of the reads on the classic page", () => {
    expect(
      pullRequestPageDecision({
        serving: [serving("server", ["GetPullRequest", "ListPullRequestFiles"])],
        failures: [],
      }),
    ).toEqual({ route: null, answered: true });
  });

  it("marks a decision no route answered, so it's checked again", () => {
    expect(
      pullRequestPageDecision({ serving: [], failures: [new TypeError("fetch failed")] }),
    ).toEqual({ route: null, answered: false });
  });
});

describe("usePullRequestPageRoute", () => {
  it("shows the classic page without asking when the flags turn on no route", () => {
    api.enabledWorkspaceRoutes.mockReturnValue([]);
    const { result } = renderRoute();
    expect(result.current).toBeNull();
    expect(api.servingRoutes).not.toHaveBeenCalled();
  });

  it("shows neither page while the host's capabilities load", async () => {
    let answer: (value: unknown) => void = () => {};
    api.servingRoutes.mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    const { result } = renderRoute();
    expect(result.current).toBe("loading");
    await act(async () => answer({ serving: [serving("server", PAGE_METHODS)], failures: [] }));
    await waitFor(() =>
      expect(result.current).toMatchObject({ route: "server", key: "server:conv_1" }),
    );
  });

  it("gives the classic page to a host without the PR methods", async () => {
    api.servingRoutes.mockResolvedValue({ serving: [serving("server", [])], failures: [] });
    const { result } = renderRoute();
    await waitFor(() => expect(result.current).toBeNull());
  });

  it("keeps its decision across renders instead of asking again", async () => {
    api.servingRoutes.mockResolvedValue({
      serving: [serving("server", PAGE_METHODS)],
      failures: [],
    });
    const { result, rerender } = renderRoute();
    await waitFor(() => expect(result.current).not.toBe("loading"));
    rerender({ id: "conv_1" });
    rerender({ id: "conv_1" });
    expect(api.servingRoutes).toHaveBeenCalledTimes(1);
  });

  it("decides again when a flag change gives other routes", async () => {
    api.servingRoutes.mockResolvedValue({
      serving: [serving("server", PAGE_METHODS)],
      failures: [],
    });
    const { result, rerender } = renderRoute();
    await waitFor(() => expect(result.current).toMatchObject({ route: "server" }));
    api.enabledWorkspaceRoutes.mockReturnValue(["direct", "server"]);
    api.servingRoutes.mockResolvedValue({
      serving: [serving("direct", PAGE_METHODS, "direct:conv_1:"), serving("server", PAGE_METHODS)],
      failures: [],
    });
    rerender({ id: "conv_1" });
    await waitFor(() => expect(result.current).toMatchObject({ route: "direct" }));
    expect(api.servingRoutes).toHaveBeenCalledTimes(2);
  });

  it("decides each session on its own", async () => {
    api.servingRoutes.mockImplementation(async (sessionId: string) =>
      sessionId === "conv_1"
        ? { serving: [serving("server", PAGE_METHODS)], failures: [] }
        : { serving: [serving("server", [], "server:conv_2")], failures: [] },
    );
    const { result, rerender } = renderRoute("conv_1");
    await waitFor(() => expect(result.current).toMatchObject({ route: "server" }));
    rerender({ id: "conv_2" });
    await waitFor(() => expect(result.current).toBeNull());
  });

  it("asks again a minute after no route answered, and shows the page once one does", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      api.servingRoutes.mockResolvedValueOnce({ serving: [], failures: [new TypeError("down")] });
      api.servingRoutes.mockResolvedValue({
        serving: [serving("server", PAGE_METHODS)],
        failures: [],
      });
      const { result } = renderRoute();
      await waitFor(() => expect(result.current).toBeNull());
      await act(async () => vi.advanceTimersByTimeAsync(60_000));
      await waitFor(() => expect(result.current).toMatchObject({ route: "server" }));
    } finally {
      vi.useRealTimers();
    }
  });

  it("says which writes the chosen route's host serves", async () => {
    api.servingRoutes.mockResolvedValue({
      serving: [serving("server", [...PAGE_METHODS, "SetReviewThreadResolved"])],
      failures: [],
    });
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(
      () => [
        usePullRequestMethodServed("conv_1", "setReviewThreadResolved"),
        usePullRequestMethodServed("conv_1", "addReaction"),
      ],
      { wrapper },
    );
    await waitFor(() => expect(result.current).toEqual([true, false]));
  });

  it("serves no writes where the session shows the classic page", () => {
    api.enabledWorkspaceRoutes.mockReturnValue([]);
    const { result } = renderHook(() => usePullRequestMethodServed("conv_1", "addReaction"), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
      ),
    });
    expect(result.current).toBe(false);
  });

  it("shows the classic page when asking fails", async () => {
    api.servingRoutes.mockRejectedValue(new Error("aborted"));
    const { result } = renderRoute();
    await waitFor(() => expect(result.current).toBeNull());
  });
});
