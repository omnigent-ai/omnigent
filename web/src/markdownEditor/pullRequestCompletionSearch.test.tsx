import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  COMPLETION_SEARCH_DELAY_MS,
  usePullRequestCompletions,
} from "./pullRequestCompletionSearch";

// The session's host, and every search it was sent with a way to answer it.
const host = vi.hoisted(() => ({
  serves: true,
  searches: [] as { session: string; text: string; answer: (numbers: number[]) => void }[],
}));

vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (_session: string, method: string) =>
    host.serves && method === "searchCompletions",
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readPullRequestRpc: (
    session: string,
    _method: string,
    call: (client: unknown) => Promise<unknown>,
  ) =>
    new Promise((resolve) => {
      let text = "";
      void call({
        searchCompletions: async (request: { text: string }) => {
          text = request.text;
          return {};
        },
      });
      host.searches.push({
        session,
        text,
        answer: (numbers) =>
          resolve({
            pull_requests: numbers.map((number) => ({
              url: `https://github.com/acme/app/pull/${number}`,
              number,
              title: `PR ${number}`,
              state: "OPEN",
              is_draft: false,
            })),
          }),
      });
    }),
}));

const PAGE = "https://github.com/acme/app/pull/1";

let queryClient: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
);

/** An editor whose menu opens on `initial`, as one does once a `#` is typed. */
function typing(initial: string | null, page: string | null = PAGE) {
  const hook = renderHook(({ typed }) => usePullRequestCompletions(page, typed, "s1"), {
    wrapper,
    initialProps: { typed: null as string | null },
  });
  hook.rerender({ typed: initial });
  return hook;
}

/** The host answers search `index` with these PRs. */
async function answer(index: number, numbers: number[]): Promise<void> {
  await act(async () => {
    host.searches[index]!.answer(numbers);
    await vi.advanceTimersByTimeAsync(0);
  });
}

async function rest(): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(COMPLETION_SEARCH_DELAY_MS);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  host.serves = true;
  host.searches = [];
  queryClient = new QueryClient();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("usePullRequestCompletions", () => {
  it("sends one scoped search once typing rests, not one per keystroke", async () => {
    const hook = typing("4");
    hook.rerender({ typed: "42" });
    await act(async () => vi.advanceTimersByTime(100));
    hook.rerender({ typed: "421" });
    expect(hook.result.current.searching).toBe(true);
    await rest();

    expect(host.searches.map(({ session, text }) => [session, text])).toEqual([["s1", "421"]]);
    await answer(0, [421]);
    expect(hook.result.current.pullRequests.map((pr) => pr.number)).toEqual([421]);
    expect(hook.result.current.searching).toBe(false);
  });

  it("searches a word too", async () => {
    typing("flaky");
    await rest();
    expect(host.searches.map(({ text }) => text)).toEqual(["flaky"]);
  });

  it("answers a query it already searched from the cache, in any editor of the PR", async () => {
    const first = typing("42");
    await rest();
    await answer(0, [42]);
    first.rerender({ typed: "4" });
    await rest();
    await answer(1, [4, 42]);
    first.rerender({ typed: "42" });
    await rest();

    const second = typing("42");
    await rest();
    expect(host.searches).toHaveLength(2);
    expect(second.result.current.pullRequests.map((pr) => pr.number)).toEqual([42]);
  });

  it("sends nothing for a bare `#`, a closed menu, or a host without the search", async () => {
    const hook = typing("");
    await rest();
    hook.rerender({ typed: "42" });
    // Closed before typing rested: the pending search never sends.
    hook.rerender({ typed: null });
    await rest();
    expect(hook.result.current.searching).toBe(false);

    host.serves = false;
    const older = typing("42");
    await rest();
    expect(older.result.current).toEqual({ pullRequests: [], searching: false });
    expect(host.searches).toEqual([]);
  });

  it("keeps a late answer for its own query after the menu closed", async () => {
    const hook = typing("42");
    await rest();
    hook.rerender({ typed: null });
    await answer(0, [42]);
    expect(hook.result.current.pullRequests).toEqual([]);

    hook.rerender({ typed: "42" });
    await rest();
    expect(host.searches).toHaveLength(1);
    expect(hook.result.current.pullRequests.map((pr) => pr.number)).toEqual([42]);
  });
});
