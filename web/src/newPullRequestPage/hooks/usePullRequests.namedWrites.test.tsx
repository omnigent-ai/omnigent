// Changes from the Pull Requests page reading a PR as one of this Mac's logins: they're made as
// that login, and when they can't be (an older shell, or support or the account gone since the
// control rendered), the click sends nothing, not on this Mac, the session's machine or as the
// host's account.

import type * as IdentityModule from "@/lib/identity";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { hostFetch } = vi.hoisted(() => ({ hostFetch: vi.fn() }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
  authenticatedFetch: hostFetch,
}));

import { MacReadAsContext } from "@/hooks/macReadAs";
import {
  markQueueChangeUnconfirmed,
  resetDesktopPullRequestWritesForTests,
  setMacWritesReleaseFlagForTests,
  unconfirmedQueueChange,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { namedReaderRefused, resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import {
  BOB,
  HOST_LOGIN,
  installNamedWriteShell,
  removeNamedWriteShell,
} from "@/test/macNamedWriteShell";

import {
  useMacWriteDispatch,
  useMacWriteRoute,
  useMergeQueueCheck,
  usePullRequestCommentComposer,
  useUpdatePullRequestLabels,
} from "./usePullRequests";

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "pull-requests-page";
const READY = { name: "ready", color: null, description: null };
const posted = (login: string) => ({
  ok: true,
  data: {
    object: "session.github.pr_comment",
    comment: { author: login, body: "hello", created_at: null, url: `${PR}#c1` },
  },
  actor: { login },
});
const labelled = (login: string) => ({ ok: true, data: { labels: [READY] }, actor: { login } });

const ALICE = { host: "github.com", login: HOST_LOGIN };
const PAGE_AS_BOB = { reader: BOB };
const PAGE_AS_ALICE = { reader: ALICE };
/** A merge options read that says where the PR stands: no merge queue. */
const NO_QUEUE = {
  object: "session.github.merge_options" as const,
  methods: ["merge" as const],
  default: "merge" as const,
  merge_queue: false,
};

function wrapperAs(reader: typeof BOB | typeof ALICE | null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      {reader ? (
        <MacReadAsContext.Provider value={reader === ALICE ? PAGE_AS_ALICE : PAGE_AS_BOB}>
          {children}
        </MacReadAsContext.Provider>
      ) : (
        children
      )}
    </QueryClientProvider>
  );
}

/** Nothing reached GitHub: neither write method, nor the session's machine. */
function expectNothingSent(shell: ReturnType<typeof installNamedWriteShell>) {
  expect(shell.codeHostWrite).not.toHaveBeenCalled();
  expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  expect(hostFetch).not.toHaveBeenCalled();
}

afterEach(() => {
  hostFetch.mockReset();
  setMacWritesReleaseFlagForTests(null);
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe("a comment from the Pull Requests page reading as its own login", () => {
  it("posts as that login, not gh's active account", async () => {
    const shell = installNamedWriteShell(() => posted("bob"));
    await shell.ready(PR);
    const { result } = renderHook(() => usePullRequestCommentComposer(SESSION, PR), {
      wrapper: wrapperAs(BOB),
    });
    act(() => result.current.setBody("hello"));
    act(() => result.current.post());
    await waitFor(() => expect(shell.codeHostWriteAs).toHaveBeenCalledTimes(1));
    expect(shell.codeHostWriteAs.mock.calls[0]![0]).toMatchObject({
      macLogin: "bob",
      actor: { login: "bob", generation: 0 },
      operation: "pull_request_comment",
    });
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    expect(hostFetch).not.toHaveBeenCalled();
  });

  it("sends nothing anywhere once the shell stopped offering it after the box rendered", async () => {
    const shell = installNamedWriteShell(() => posted("bob"));
    await shell.ready(PR);
    const { result } = renderHook(() => usePullRequestCommentComposer(SESSION, PR), {
      wrapper: wrapperAs(BOB),
    });
    act(() => result.current.setBody("hello"));
    await act(() => shell.setWriteAs(false));
    act(() => result.current.post());
    await waitFor(() =>
      expect(result.current.draft.error).toBe(
        "Update the app to make changes as @bob here. Nothing was changed.",
      ),
    );
    expect(result.current.draft.body).toBe("hello");
    expectNothingSent(shell);
  });

  it("sends nothing anywhere once main refused to read as the login", async () => {
    const shell = installNamedWriteShell(() => posted("bob"));
    await shell.ready(PR);
    const { result } = renderHook(() => usePullRequestCommentComposer(SESSION, PR), {
      wrapper: wrapperAs(BOB),
    });
    act(() => result.current.setBody("hello"));
    act(() => namedReaderRefused(BOB));
    act(() => result.current.post());
    await waitFor(() => expect(result.current.draft.error).toMatch(/as @bob on this Mac/));
    expectNothingSent(shell);
  });
});

describe("a routed change from the Pull Requests page reading as its own login", () => {
  it("is made as that login", async () => {
    const shell = installNamedWriteShell(() => labelled("bob"));
    await shell.ready(PR);
    const { result } = renderHook(() => useUpdatePullRequestLabels(SESSION), {
      wrapper: wrapperAs(BOB),
    });
    act(() => result.current.mutate({ prUrl: PR, add: [READY], remove: [] }));
    await waitFor(() => expect(shell.codeHostWriteAs).toHaveBeenCalledTimes(1));
    expect(shell.codeHostWriteAs.mock.calls[0]![0]).toMatchObject({ macLogin: "bob" });
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    expect(hostFetch).not.toHaveBeenCalled();
  });

  it.each([
    ["an older shell", { writeAs: false }],
    ["a bridge without codeHostWriteAs", { writeAsMethod: false }],
  ])("on %s, says why and sends nothing, never on the session's machine", async (_l, options) => {
    const shell = installNamedWriteShell(() => labelled(HOST_LOGIN), options);
    await shell.ready(PR);
    const onError = vi.fn();
    const { result } = renderHook(() => useUpdatePullRequestLabels(SESSION), {
      wrapper: wrapperAs(BOB),
    });
    act(() => result.current.mutate({ prUrl: PR, add: [READY], remove: [] }, { onError }));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe(
      "Update the app to make changes as @bob here. Nothing was changed.",
    );
    expect(onError).toHaveBeenCalledTimes(1);
    expectNothingSent(shell);
  });

  it("sends nothing anywhere when support went away between render and click", async () => {
    const shell = installNamedWriteShell(() => labelled("bob"));
    await shell.ready(PR);
    const { result } = renderHook(() => useUpdatePullRequestLabels(SESSION), {
      wrapper: wrapperAs(BOB),
    });
    await act(() => shell.setWriteAs(false));
    act(() => result.current.mutate({ prUrl: PR, add: [READY], remove: [] }));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expectNothingSent(shell);
  });

  it("outside the page, on the same older shell, still writes as the host's account", async () => {
    const shell = installNamedWriteShell(() => labelled(HOST_LOGIN), { writeAs: false });
    await shell.ready(PR);
    const { result } = renderHook(() => useUpdatePullRequestLabels(SESSION), {
      wrapper: wrapperAs(null),
    });
    act(() => result.current.mutate({ prUrl: PR, add: [READY], remove: [] }));
    await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(1));
    const [request] = shell.codeHostWrite.mock.calls[0]!;
    expect(request).not.toHaveProperty("macLogin");
    expect(request.actor).toEqual({ login: HOST_LOGIN, generation: 1 });
    expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  });
});

describe("a change rendered for the page's own login, after the shell lost named support", () => {
  it("sends nothing anywhere once the shell reads and writes as nobody by name", async () => {
    const shell = installNamedWriteShell(() => labelled(HOST_LOGIN));
    await shell.ready(PR);
    const { result } = renderHook(() => useUpdatePullRequestLabels(SESSION), {
      wrapper: wrapperAs(BOB),
    });
    // Neither readAs nor writeAs now, as on an older shell: still Bob's change, so it's blocked.
    await act(() => shell.loseNamedSupport());
    act(() => result.current.mutate({ prUrl: PR, add: [READY], remove: [] }));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe(
      "Update the app to make changes as @bob here. Nothing was changed.",
    );
    expectNothingSent(shell);
  });
});

describe("the route a mounted control shows for the page's own login", () => {
  it("follows the login's first proven read, its refusal, and a sign-in again", async () => {
    const shell = installNamedWriteShell();
    await shell.ready(PR, { asBob: false });
    const { result } = renderHook(
      () => ({ route: useMacWriteRoute(PR), dispatch: useMacWriteDispatch(PR) }),
      { wrapper: wrapperAs(BOB) },
    );
    expect(result.current.route).toBeNull();
    expect(result.current.dispatch).toMatchObject({
      kind: "blocked",
      unavailable: { reason: "account_checking" },
    });
    await act(() => shell.readAsBob(PR));
    await waitFor(() =>
      expect(result.current.route?.actor).toEqual({ login: "bob", generation: 0 }),
    );
    expect(result.current.dispatch.kind).toBe("mac");
    act(() => namedReaderRefused(BOB));
    await waitFor(() => expect(result.current.route).toBeNull());
    expect(result.current.dispatch).toMatchObject({
      kind: "blocked",
      unavailable: { reason: "no_account" },
    });
    shell.state.bobGeneration = 3;
    await act(() => shell.readAsBob(PR));
    await waitFor(() =>
      expect(result.current.route?.actor).toEqual({ login: "bob", generation: 3 }),
    );
    expect(result.current.dispatch.kind).toBe("mac");
  });
});

describe("Check again for a merge queue change sent as the page's own login", () => {
  const SENT_AS_BOB = { login: "bob", reader: BOB };

  /** A check whose read answers when the test says. */
  function pausedRefetch() {
    let answer!: () => void;
    const refetch = vi.fn(
      () =>
        new Promise<{ isSuccess: boolean; data: typeof NO_QUEUE; dataUpdatedAt: number }>(
          (resolve) => {
            answer = () =>
              resolve({ isSuccess: true, data: NO_QUEUE, dataUpdatedAt: Date.now() + 1 });
          },
        ),
    );
    return { refetch, answer: () => answer() };
  }
  const freshRead = async () => ({
    isSuccess: true,
    data: NO_QUEUE,
    dataUpdatedAt: Date.now() + 1,
  });

  it("stays held when checked as the host's account", async () => {
    const shell = installNamedWriteShell();
    await shell.ready(PR);
    markQueueChangeUnconfirmed("user-1", PR, "pull_request_merge_queue_add", SENT_AS_BOB);
    const refetch = vi.fn(freshRead);
    const { result } = renderHook(() => useMergeQueueCheck(PR, refetch), {
      wrapper: wrapperAs(null),
    });
    await act(() => result.current.check());
    expect(unconfirmedQueueChange(PR)).not.toBeNull();
    expect(result.current.failed).toBe(true);
    // It didn't even read: an answer as another account says nothing about Bob's change.
    expect(refetch).not.toHaveBeenCalled();
    expectNothingSent(shell);
  });

  it("is released by a check as the login after it signed in again", async () => {
    const shell = installNamedWriteShell();
    await shell.ready(PR);
    markQueueChangeUnconfirmed("user-1", PR, "pull_request_merge_queue_add", SENT_AS_BOB);
    shell.bobSignsInAgain(2);
    await shell.readAsBob(PR);
    const { result } = renderHook(() => useMergeQueueCheck(PR, freshRead), {
      wrapper: wrapperAs(BOB),
    });
    await act(() => result.current.check());
    expect(unconfirmedQueueChange(PR)).toBeNull();
    expectNothingSent(shell);
  });

  it("stays held when the login signs in again while the check reads", async () => {
    const shell = installNamedWriteShell();
    await shell.ready(PR);
    markQueueChangeUnconfirmed("user-1", PR, "pull_request_merge_queue_add", SENT_AS_BOB);
    const { refetch, answer } = pausedRefetch();
    const { result } = renderHook(() => useMergeQueueCheck(PR, refetch), {
      wrapper: wrapperAs(BOB),
    });
    let done!: Promise<void>;
    act(() => {
      done = result.current.check();
    });
    shell.bobSignsInAgain(2);
    await act(() => shell.readAsBob(PR));
    await act(async () => {
      answer();
      await done;
    });
    expect(unconfirmedQueueChange(PR)).not.toBeNull();
    expect(result.current.failed).toBe(true);
    expectNothingSent(shell);
  });
});

describe("a change picked as gh's active account on a shell that can't write by name", () => {
  it("sends nothing, before or after gh switched to another account", async () => {
    const shell = installNamedWriteShell(() => labelled(HOST_LOGIN), { writeAs: false });
    await shell.ready(PR);
    const { result } = renderHook(() => useUpdatePullRequestLabels(SESSION), {
      wrapper: wrapperAs(ALICE),
    });
    const onError = vi.fn();
    act(() => result.current.mutate({ prUrl: PR, add: [READY], remove: [] }, { onError }));
    await waitFor(() => expect(onError).toHaveBeenCalledTimes(1));
    expect(result.current.error?.message).toBe(
      "Update the app to make changes as @alice here. Nothing was changed.",
    );
    // Never as the next active account either, once gh switched.
    shell.switchActiveAccount("carol");
    await act(() => shell.readAsHost(PR));
    act(() => result.current.mutate({ prUrl: PR, add: [READY], remove: [] }, { onError }));
    await waitFor(() => expect(onError).toHaveBeenCalledTimes(2));
    expectNothingSent(shell);
  });
});
