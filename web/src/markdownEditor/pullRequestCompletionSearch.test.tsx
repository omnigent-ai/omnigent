import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import type * as DesktopReadPath from "@/lib/desktopReadPath";
import { advanceMacReadScope, macNamedReadScopeKey, macReadScopeKey } from "@/lib/macReadScope";
import { bindMacReadScopes } from "@/lib/macReadScopeQueries";

import {
  COMPLETION_SEARCH_DELAY_MS,
  PULL_REQUEST_COMPLETION_SEARCH_OPERATION,
  usePullRequestCompletions,
} from "./pullRequestCompletionSearch";

// This Mac's shell, and every search it was sent with a way to answer it.
const mac = vi.hoisted(() => ({
  offers: true,
  reader: null as { host: string; login: string } | null,
  searches: [] as {
    url: string;
    readAs: string | null;
    operation: string;
    query: string;
    answer: (numbers: number[]) => void;
  }[],
}));

vi.mock("@/lib/desktopReadPath", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopReadPath>()),
  macReadTargetAs: (url: string | null, readAs: { login: string } | null) => {
    const target = parseCodeHostPullRequestUrl(url);
    return target && readAs ? { ...target, readAs: readAs.login } : target;
  },
  macOffers: (_provider: string, operation: string) =>
    mac.offers && operation === PULL_REQUEST_COMPLETION_SEARCH_OPERATION,
  // The app's own scope keys, so a shell account change drops these answers as it does any read.
  readScope: (url: string, readAs: { login: string } | null) =>
    readAs
      ? macNamedReadScopeKey(new URL(url).hostname, readAs.login)
      : macReadScopeKey("github", new URL(url).hostname),
}));
vi.mock("@/hooks/macReadAs", () => ({ useMacReadAs: () => mac.reader }));
vi.mock("@/lib/desktopPullRequests", () => ({
  readOnThisMac: (
    target: { url: string; readAs?: string },
    operation: string,
    params: { repository: string; text: string },
  ) =>
    new Promise((resolve) => {
      mac.searches.push({
        url: target.url,
        readAs: target.readAs ?? null,
        operation,
        query: `${params.repository} ${params.text}`,
        answer: (numbers) =>
          resolve({
            data: {
              viewer: "octo",
              search: {
                pull_requests: numbers.map((number) => ({
                  url: `https://github.com/acme/app/pull/${number}`,
                  number,
                  title: `PR ${number}`,
                  state: "OPEN",
                  is_draft: false,
                })),
              },
            },
            login: null,
          }),
      });
    }),
}));

const PAGE = "https://github.com/acme/app/pull/1";

let queryClient: QueryClient;
let unbind: () => void;
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
);

/** An editor whose menu opens on `initial`, as one does once a `#` is typed. */
function typing(initial: string | null, page: string | null = PAGE) {
  const hook = renderHook(({ typed }) => usePullRequestCompletions(page, typed), {
    wrapper,
    initialProps: { typed: null as string | null },
  });
  hook.rerender({ typed: initial });
  return hook;
}

/** The shell answers search `index` with these PRs. */
async function answer(index: number, numbers: number[]): Promise<void> {
  await act(async () => {
    mac.searches[index]!.answer(numbers);
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
  mac.offers = true;
  mac.reader = null;
  mac.searches = [];
  queryClient = new QueryClient();
  unbind = bindMacReadScopes(queryClient);
});
afterEach(() => {
  unbind();
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

    expect(mac.searches.map(({ operation, query }) => [operation, query])).toEqual([
      [PULL_REQUEST_COMPLETION_SEARCH_OPERATION, "acme/app 421"],
    ]);
    await answer(0, [421]);
    expect(hook.result.current.pullRequests.map((pr) => pr.number)).toEqual([421]);
    expect(hook.result.current.searching).toBe(false);
  });

  it("searches a word too", async () => {
    typing("flaky");
    await rest();
    expect(mac.searches.map(({ query }) => query)).toEqual(["acme/app flaky"]);
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
    expect(mac.searches).toHaveLength(2);
    expect(second.result.current.pullRequests.map((pr) => pr.number)).toEqual([42]);
  });

  it("sends nothing for a bare `#`, a closed menu, or a shell without the search", async () => {
    const hook = typing("");
    await rest();
    hook.rerender({ typed: "42" });
    // Closed before typing rested: the pending search never sends.
    hook.rerender({ typed: null });
    await rest();
    expect(hook.result.current.searching).toBe(false);

    mac.offers = false;
    const older = typing("42");
    await rest();
    expect(older.result.current).toEqual({ pullRequests: [], searching: false });
    expect(mac.searches).toEqual([]);
  });

  it("keeps a late answer for its own query after the menu closed", async () => {
    const hook = typing("42");
    await rest();
    hook.rerender({ typed: null });
    await answer(0, [42]);
    expect(hook.result.current.pullRequests).toEqual([]);

    hook.rerender({ typed: "42" });
    await rest();
    expect(mac.searches).toHaveLength(1);
    expect(hook.result.current.pullRequests.map((pr) => pr.number)).toEqual([42]);
  });

  it("never shows one account's answers as another's, even after the menu closed", async () => {
    mac.reader = { host: "github.com", login: "alice" };
    const hook = typing("42");
    await rest();
    await answer(0, [42]);
    expect(hook.result.current.pullRequests.map((pr) => pr.number)).toEqual([42]);
    hook.rerender({ typed: null });
    await rest();

    mac.reader = { host: "github.com", login: "bob" };
    hook.rerender({ typed: "42" });
    await rest();
    // Bob's own search is still on its way: nothing of Alice's shows meanwhile.
    expect(mac.searches.map(({ readAs }) => readAs)).toEqual(["alice", "bob"]);
    expect(hook.result.current.pullRequests).toEqual([]);
    expect(hook.result.current.searching).toBe(true);
  });

  it("never shows the previous account's answers once the shell's account changes", async () => {
    const hook = typing("42");
    await rest();
    await answer(0, [42]);
    hook.rerender({ typed: null });
    await rest();

    // The shell says this Mac now reads GitHub as another account, as the app hears it.
    act(() => {
      advanceMacReadScope(macReadScopeKey("github", "github.com"));
    });
    hook.rerender({ typed: "42" });
    await rest();
    expect(mac.searches).toHaveLength(2);
    expect(hook.result.current.pullRequests).toEqual([]);
    expect(hook.result.current.searching).toBe(true);
  });
});
