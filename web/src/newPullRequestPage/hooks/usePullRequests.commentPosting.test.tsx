// Posting a PR comment through the session's host: the draft says Posting… with its text until
// the PR's next read shows the comment, and GitHub's answer is never written into any cache.

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

const { hostFetch } = vi.hoisted(() => ({ hostFetch: vi.fn() }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
  authenticatedFetch: hostFetch,
}));

import {
  POSTED_COMMENT_READ_WAIT_MS,
  usePullRequestCommentComposer,
  type PullRequestComment,
  type PullRequestInfo,
} from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const SESSION = "conv_1";
const PR = "https://github.com/acme/app/pull/7";
const HOST_KEY = ["pr-page-github-info", SESSION, PR];
// The same PR as this Mac reads it: a host post must never write into it.
const MAC_KEY = ["pr-page-github-info", SESSION, PR, "mac-read", "user-1", "github", "github.com"];
// The session's own read, which names the PR it selected rather than keying by it.
const SESSION_KEY = ["pr-page-github-info", SESSION];
const POSTED: PullRequestComment = {
  author: "host-octo",
  body: "hello",
  created_at: "2026-11-18T10:00:00Z",
  url: `${PR}#issuecomment-1`,
};

function info(comments: PullRequestComment[]): PullRequestInfo {
  return {
    available: true,
    selected_pr_url: PR,
    pr: { url: PR, comments },
  } as unknown as PullRequestInfo;
}

const bodies = (client: QueryClient, key: readonly unknown[]) =>
  client.getQueryData<PullRequestInfo>(key)?.pr?.comments?.map((comment) => comment.body) ?? [];

/** The host's answer to each post, as the test sets it; every post is counted. */
let answerPost: () => Promise<Response>;
let posts: unknown[];

beforeEach(() => {
  posts = [];
  answerPost = async () => new Response(JSON.stringify({ comment: POSTED }), { status: 200 });
  hostFetch.mockReset();
  hostFetch.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const { pathname } = new URL(String(input), "http://localhost");
    if (pathname.endsWith("/github/comments")) {
      posts.push(JSON.parse(String(init?.body)).body);
      return answerPost();
    }
    return Promise.resolve(new Response("{}", { status: 404 }));
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** The composer beside the panel's host read of the PR, whose reads the test answers. */
function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(MAC_KEY, info([]));
  client.setQueryData(SESSION_KEY, info([]));
  const reads: { resolve: (value: PullRequestInfo) => void }[] = [];
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(
    () => {
      useQuery({
        queryKey: HOST_KEY,
        queryFn: () =>
          new Promise<PullRequestInfo>((resolve) => {
            reads.push({ resolve });
          }),
      });
      return usePullRequestCommentComposer(SESSION, PR);
    },
    { wrapper },
  );
  return { client, result, reads };
}

it("says Posting… with the sent text until the next read shows it, writing no cache", async () => {
  const { client, result, reads } = setup();
  await waitFor(() => expect(reads).toHaveLength(1));
  await act(async () => reads[0].resolve(info([])));

  await act(async () => result.current.setBody("hello"));
  await act(async () => result.current.post());
  expect(result.current.draft).toMatchObject({ body: "hello", pending: true });

  // GitHub took it, and the PR is read again: until that read lands, the draft keeps waiting.
  await waitFor(() => expect(reads).toHaveLength(2));
  expect(result.current.draft).toMatchObject({ body: "hello", pending: true, error: null });
  expect(bodies(client, HOST_KEY)).toEqual([]);
  expect(bodies(client, MAC_KEY)).toEqual([]);

  await act(async () => reads[1].resolve(info([POSTED])));
  await waitFor(() => expect(result.current.draft.pending).toBe(false));
  expect(result.current.draft.body).toBe("");
  expect(bodies(client, HOST_KEY)).toEqual(["hello"]);
  // Only its own read brought it; this Mac's read of the same PR was never written.
  expect(bodies(client, MAC_KEY)).toEqual([]);
  // The session's read of the same PR is read again too, and wasn't written either.
  expect(bodies(client, SESSION_KEY)).toEqual([]);
  expect(client.getQueryState(SESSION_KEY)?.isInvalidated).toBe(true);
  expect(posts).toEqual(["hello"]);
});

it("stops waiting for a read that doesn't come, so a posted comment's draft still clears", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const { result, reads } = setup();
  await vi.waitFor(() => expect(reads).toHaveLength(1));
  await act(async () => reads[0].resolve(info([])));
  await act(async () => result.current.setBody("hello"));
  await act(async () => result.current.post());
  await vi.waitFor(() => expect(reads).toHaveLength(2));
  expect(result.current.draft.pending).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(POSTED_COMMENT_READ_WAIT_MS));
  await vi.waitFor(() => expect(result.current.draft.pending).toBe(false));
  expect(result.current.draft.body).toBe("");
});

it("sends one post however often it's asked while one is on its way", async () => {
  let release!: () => void;
  answerPost = () =>
    new Promise((resolve) => {
      release = () => resolve(new Response(JSON.stringify({ comment: POSTED }), { status: 200 }));
    });
  const { result, reads } = setup();
  await waitFor(() => expect(reads).toHaveLength(1));
  await act(async () => reads[0].resolve(info([])));
  await act(async () => result.current.setBody("hello"));
  // Twice in one task, as a repeated ⌘⏎ is, then again once it shows Posting….
  await act(async () => {
    result.current.post();
    result.current.post();
  });
  await act(async () => result.current.post());
  await waitFor(() => expect(posts).toHaveLength(1));
  await act(async () => release());
  await waitFor(() => expect(reads).toHaveLength(2));
  await act(async () => result.current.post());
  expect(posts).toEqual(["hello"]);
});

it.each([
  ["refuses it", 422, "Validation failed"],
  ["fails with an error that may have sent it", 502, "Bad gateway"],
])(
  "puts the text back, with what was added meanwhile, when the host %s",
  async (_, status, detail) => {
    let release!: () => void;
    answerPost = () =>
      new Promise((resolve) => {
        release = () => resolve(new Response(JSON.stringify({ detail }), { status }));
      });
    const { result, reads } = setup();
    await waitFor(() => expect(reads).toHaveLength(1));
    await act(async () => result.current.setBody("hello"));
    await act(async () => result.current.post());
    await waitFor(() => expect(posts).toHaveLength(1));
    // A quote added while it posts, as Quote Reply does.
    await act(async () => result.current.setBody("hello\n\n> quoted"));
    await act(async () => release());
    await waitFor(() => expect(result.current.draft.pending).toBe(false));
    expect(result.current.draft.body).toBe("hello\n\n> quoted");
    expect(result.current.draft.error).toContain(detail);
    if (status >= 500) expect(result.current.draft.error).toMatch(/Check GitHub before retrying/);
    expect(posts).toEqual(["hello"]);
  },
);
