import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import type * as WorkspaceChangedFiles from "./useWorkspaceChangedFiles";
import { usePullRequestInfos, usePullRequestReadPriority } from "./usePullRequests";

vi.mock("./useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceChangedFiles>()),
  useWorkspaceServeable: () => true,
}));

afterEach(() => vi.unstubAllGlobals());

interface HeldRead {
  prUrl: string | null;
  signal: AbortSignal | null | undefined;
  answer: () => void;
}

/** Stubs `fetch` so each status read stays open until the test answers it. */
function holdReads({ ignoreAbort = false } = {}): HeldRead[] {
  const held: HeldRead[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(
      (input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((resolve, reject) => {
          if (!ignoreAbort) {
            init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
          }
          held.push({
            prUrl: new URL(String(input), "http://localhost").searchParams.get("pr_url"),
            signal: init?.signal,
            answer: () =>
              resolve(
                new Response(
                  JSON.stringify({ object: "session.github.info", available: true, pr: null }),
                ),
              ),
          });
        }),
    ),
  );
  return held;
}

function pullRequestUrls(count: number, first = 1): string[] {
  return Array.from(
    { length: count },
    (_, index) => `https://github.com/team/project/pull/${first + index}`,
  );
}

function withQueryClient() {
  const client = new QueryClient();
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

it("reads a session's linked pull requests two at a time, in order", async () => {
  const held = holdReads();
  const urls = pullRequestUrls(5);
  const { unmount } = renderHook(() => usePullRequestInfos("conv-1", urls), {
    wrapper: withQueryClient(),
  });
  await waitFor(() => expect(held.map((read) => read.prUrl)).toEqual(urls.slice(0, 2)));
  act(() => held[0]!.answer());
  await waitFor(() => expect(held.map((read) => read.prUrl)).toEqual(urls.slice(0, 3)));
  unmount();
});

it("reads the pull request being viewed ahead of the queue", async () => {
  const held = holdReads();
  const urls = pullRequestUrls(6);
  const initialProps: { viewed: string | undefined } = { viewed: urls[4] };
  const { rerender, unmount } = renderHook(
    ({ viewed }) => {
      usePullRequestInfos("conv-1", urls);
      usePullRequestReadPriority("conv-1", viewed);
    },
    { wrapper: withQueryClient(), initialProps },
  );
  await waitFor(() =>
    expect(held.map((read) => read.prUrl).sort()).toEqual([urls[0], urls[1], urls[4]].sort()),
  );
  // Leaving while the viewed read is still open (and still polled) keeps it on the extra
  // slot, so viewing another pull request waits for a free slot instead of adding a read.
  rerender({ viewed: undefined });
  rerender({ viewed: urls[5] });
  await act(async () => {});
  expect(held).toHaveLength(3);
  act(() => held[0]!.answer());
  await waitFor(() =>
    expect(held.map((read) => read.prUrl)).toEqual([urls[0], urls[1], urls[4], urls[5]]),
  );
  unmount();
});

it("shares the limit across sessions", async () => {
  const held = holdReads();
  const first = pullRequestUrls(2);
  const [other] = pullRequestUrls(1, 9);
  const { unmount } = renderHook(
    () => {
      usePullRequestInfos("conv-1", first);
      usePullRequestInfos("conv-2", [other!]);
    },
    { wrapper: withQueryClient() },
  );
  await waitFor(() => expect(held.map((read) => read.prUrl)).toEqual(first));
  act(() => held[0]!.answer());
  await waitFor(() => expect(held.map((read) => read.prUrl)).toEqual([...first, other]));
  unmount();
});

it("gives up a closed session's place in the queue, even for reads that never settle", async () => {
  const held = holdReads({ ignoreAbort: true });
  const urls = pullRequestUrls(4);
  const closed = renderHook(() => usePullRequestInfos("conv-1", urls), {
    wrapper: withQueryClient(),
  });
  await waitFor(() => expect(held).toHaveLength(2));
  closed.unmount();
  await waitFor(() => expect(held.every((read) => read.signal?.aborted)).toBe(true));
  const [next] = pullRequestUrls(1, 9);
  const open = renderHook(() => usePullRequestInfos("conv-2", [next!]), {
    wrapper: withQueryClient(),
  });
  // The closed session's two waiting reads never start.
  await waitFor(() => expect(held.map((read) => read.prUrl)).toEqual([...urls.slice(0, 2), next]));
  open.unmount();
});
