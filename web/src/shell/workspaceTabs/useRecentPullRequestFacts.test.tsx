import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PullRequest, PullRequestAssociation, PullRequestInfo } from "@/hooks/usePullRequests";
import type * as WorkspaceChangedFiles from "@/hooks/useWorkspaceChangedFiles";
import { sessionPullRequestsKey, type StoredPullRequests } from "@/hooks/useSessionPullRequests";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";

import { usePullRequestInfo } from "@/hooks/usePullRequests";

import { useRecentPullRequestFacts } from "./useRecentPullRequestFacts";

const host = vi.hoisted(() => ({ serveable: true as boolean }));

vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceChangedFiles>()),
  useWorkspaceServeable: () => host.serveable,
}));

const URL_338 = "https://github.com/example-org/widgets/pull/338";
const URL_321 = "https://github.com/example-org/widgets/pull/321";

const association = (url: string, number: number, title: string): PullRequestAssociation => ({
  url,
  host: "github.com",
  repository: "example-org/widgets",
  number,
  title,
  relationship: "created",
});

const sessionInfo = (prs: PullRequestAssociation[]): PullRequestInfo =>
  ({
    object: "session.github.info",
    available: true,
    tracking_available: true,
    prs,
    pr: null,
  }) as PullRequestInfo;

const detailInfo = (pr: Partial<PullRequest> & { url: string }): PullRequestInfo =>
  ({ ...sessionInfo([]), selected_pr_url: pr.url, pr: pr as PullRequest }) as PullRequestInfo;

let requests: string[];

beforeEach(() => {
  host.serveable = true;
  requests = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      requests.push(String(input));
      return new Response("not stubbed", { status: 500 });
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

function setup(initial: { conversationId: string; urls: string[] }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, session_pull_requests: true }}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </CapabilitiesProvider>
  );
  const hook = renderHook(
    ({ conversationId, urls }) => useRecentPullRequestFacts(conversationId, urls),
    { initialProps: initial, wrapper },
  );
  return { client, ...hook };
}

const prRequests = () => requests.filter((request) => request.includes("pr_url="));

describe("useRecentPullRequestFacts", () => {
  it("names PRs from the server's stored list while the host is unavailable", () => {
    host.serveable = false;
    const client = new QueryClient();
    client.setQueryData<StoredPullRequests>(sessionPullRequestsKey("conv"), {
      prs: [association(URL_338, 338, "Show who wrote each review comment")],
      synced: true,
    });
    const { result } = renderHook(() => useRecentPullRequestFacts("conv", [URL_338]), {
      wrapper: ({ children }) => (
        <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, session_pull_requests: true }}>
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        </CapabilitiesProvider>
      ),
    });
    expect(result.current.linked.map((pr) => pr.title)).toEqual([
      "Show who wrote each review comment",
    ]);
    expect(prRequests()).toEqual([]);
  });

  it("starts from nothing on a cold cache and follows details another view reads", async () => {
    const { client, result } = setup({ conversationId: "conv", urls: [URL_338, URL_321] });
    expect(result.current.details.size).toBe(0);
    act(() => {
      client.setQueryData(
        ["github-info", "conv", URL_321],
        detailInfo({ url: URL_321, number: 321, title: "Retry uploads", author: "sam-lee" }),
      );
    });
    await waitFor(() =>
      expect(result.current.details.get(URL_321)).toMatchObject({ author: "sam-lee" }),
    );
    act(() => {
      client.setQueryData(
        ["github-info", "conv", URL_321],
        detailInfo({ url: URL_321, number: 321, title: "Retry flaky uploads", author: "sam-lee" }),
      );
    });
    await waitFor(() =>
      expect(result.current.details.get(URL_321)?.title).toBe("Retry flaky uploads"),
    );
    // Every recent PR is looked up, but only in the cache.
    expect(prRequests()).toEqual([]);
  });

  it("says when each PR's details were read, as the cache does", async () => {
    const { client, result } = setup({ conversationId: "conv", urls: [URL_321] });
    const readAt = Date.UTC(2026, 0, 1, 10, 0);
    act(() => {
      client.setQueryData(
        ["github-info", "conv", URL_321],
        detailInfo({ url: URL_321, number: 321, title: "Retry uploads", author: "sam-lee" }),
        { updatedAt: readAt },
      );
    });
    await waitFor(() => expect(result.current.detailsUpdatedAt?.get(URL_321)).toBe(readAt));
  });

  it("reads the selected PR's details from the session's own info", async () => {
    const { client, result } = setup({ conversationId: "conv", urls: [] });
    act(() => {
      client.setQueryData(["github-info", "conv"], {
        ...sessionInfo([association(URL_338, 338, "Show who wrote each review comment")]),
        pr: {
          url: URL_338,
          number: 338,
          title: "Show who wrote each review comment",
          author: "jane-doe",
        },
      });
    });
    await waitFor(() => expect(result.current.linked).toHaveLength(1));
    expect(result.current.details.get(URL_338)).toMatchObject({ author: "jane-doe" });
  });

  it("keeps the later read when the session's info and the PR's own query both hold its details", async () => {
    const earlier = Date.UTC(2026, 0, 1, 10, 0);
    const later = Date.UTC(2026, 0, 1, 11, 0);
    const read = (state: "OPEN" | "MERGED") =>
      ({ url: URL_338, number: 338, title: "Show authors", author: "jane-doe", state }) as const;
    const readBoth = (session: number, own: number) => {
      const { client, result, unmount } = setup({ conversationId: "conv", urls: [URL_338] });
      act(() => {
        client.setQueryData(
          ["github-info", "conv"],
          { ...sessionInfo([]), pr: read(session > own ? "MERGED" : "OPEN") as PullRequest },
          { updatedAt: session },
        );
        client.setQueryData(
          ["github-info", "conv", URL_338],
          detailInfo(read(own > session ? "MERGED" : "OPEN")),
          { updatedAt: own },
        );
      });
      return { result, unmount };
    };

    // The session's info read the merge after the PR's own query last ran.
    const sessionLater = readBoth(later, earlier);
    await waitFor(() =>
      expect(sessionLater.result.current.details.get(URL_338)?.state).toBe("MERGED"),
    );
    expect(sessionLater.result.current.detailsUpdatedAt?.get(URL_338)).toBe(later);
    sessionLater.unmount();

    // And the other way round.
    const ownLater = readBoth(earlier, later);
    await waitFor(() => expect(ownLater.result.current.details.get(URL_338)?.state).toBe("MERGED"));
    expect(ownLater.result.current.detailsUpdatedAt?.get(URL_338)).toBe(later);
  });

  it("drops details cached for another session", async () => {
    const { client, result, rerender } = setup({ conversationId: "conv", urls: [URL_338] });
    act(() => {
      client.setQueryData(
        ["github-info", "conv", URL_338],
        detailInfo({ url: URL_338, number: 338, title: "Mine", author: "jane-doe" }),
      );
    });
    await waitFor(() => expect(result.current.details.get(URL_338)?.title).toBe("Mine"));
    rerender({ conversationId: "other", urls: [URL_338] });
    expect(result.current.details.has(URL_338)).toBe(false);
  });

  it("leaves the PR's own query fetching from the host after an invalidation", async () => {
    const detail = detailInfo({
      url: URL_338,
      number: 338,
      title: "From the host",
      author: "jane-doe",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        requests.push(String(input));
        const body = String(input).includes("pr_url=") ? detail : sessionInfo([]);
        return new Response(JSON.stringify(body), { status: 200 });
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(
      () => ({
        // The Pulls panel reading this PR, beside the new-tab page's rows.
        live: usePullRequestInfo("conv", { prUrl: URL_338 }),
        facts: useRecentPullRequestFacts("conv", [URL_338]),
      }),
      {
        wrapper: ({ children }) => (
          <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, session_pull_requests: true }}>
            <QueryClientProvider client={client}>{children}</QueryClientProvider>
          </CapabilitiesProvider>
        ),
      },
    );
    await waitFor(() => expect(result.current.facts.details.get(URL_338)?.author).toBe("jane-doe"));
    const before = prRequests().length;

    await act(() => client.invalidateQueries({ queryKey: ["github-info", "conv"] }));

    await waitFor(() => expect(prRequests().length).toBeGreaterThan(before));
    await waitFor(() => expect(result.current.live.isFetching).toBe(false));
    expect(result.current.live.isError).toBe(false);
    expect(result.current.live.data?.pr?.title).toBe("From the host");
  });
});
