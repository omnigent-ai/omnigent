// A PR's review conversations read on this Mac and kept on this device: after a restart they
// show at once with an honest age, the age of the oldest part still shown, and say they're kept
// until every part kept on disk has been read again. Driven through the real hook, the shell's
// read transport and the persister, across restarts, in the orders reads can land.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as PullRequestsModule from "@/newPullRequestPage/hooks/usePullRequests";
import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";

const state = vi.hoisted(() => ({
  user: "user-1" as string | null,
  server: "https://server-a.example.com" as string | null,
}));

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => state.user,
  subscribeIdentity: () => () => {},
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => state.server,
}));
vi.mock("@/newPullRequestPage/hooks/usePullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof PullRequestsModule>()),
  // The page's reads on this Mac's own github.com account, as the shell settles them; a view
  // reads again as the scope's reader moves, as the real hook does.
  useDesktopPullRequestReads: () => {
    useMacReadScopeVersion();
    return {
      scope: () => macReadScopeKey("github", "github.com"),
      target: (url: string | undefined) => (url ? pullRequest(url) : null),
    };
  },
}));

import { usePullRequestReviewThreads } from "@/newPullRequestPage/hooks/usePullRequestReviewThreads";
import type { CodeHostPullRequest } from "@/lib/codeHosts";
import {
  desktopReadTarget,
  githubMacReadsPreference,
  GITHUB_MAC_READ_OPERATIONS,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { githubCooldowns } from "@/lib/githubCooldown";
import {
  acceptMacRead,
  beginMacRead,
  macReadScopeKey,
  resetMacReadScopesForTests,
  useMacReadScopeVersion,
} from "@/lib/macReadScope";
import { bindMacReadScopes } from "@/lib/macReadScopeQueries";
import { resetPullRequestDetailsCacheForTests } from "@/lib/pullRequestDetailsCache";
import { pullRequestFreshness } from "@/lib/pullRequestFreshness";
import type {
  ReviewComment,
  ReviewThread,
  ReviewThreadsPage,
} from "@/lib/pullRequestReviewThreads";

import { changeComment } from "./usePullRequestReviewThreads.saved.writes";

const STORAGE_PREFIX = "omnigent:pull-request-details-";
const BASE = "a".repeat(40);
const HEAD = "b".repeat(40);
const REVISION = `${BASE}:${HEAD}`;
const URL_ONE = "https://github.com/acme/app/pull/1";
const MINUTE = 60_000;

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

interface ShellAnswer {
  ok: true;
  data: unknown;
  identity: { host: string; login: string; generation: number };
}

interface ShellRead {
  operation: string;
  params: Record<string, unknown>;
  answer: Deferred<ShellAnswer>;
}

/** A shell reading GitHub as alice; reads wait until a test answers them. */
const shell = { reads: [] as ShellRead[] };

/**
 * Whether the shell names who read each answer. Where it does, a PR's conversations are one
 * shared record for every view; where it doesn't, each session keeps its own.
 */
let proves = false;

/** The account this Mac reads github.com as, and its sign-in; a switch is a new sign-in. */
const account = { login: "alice", generation: 1 };

const answer = (data: unknown): ShellAnswer => ({
  ok: true,
  identity: { host: "github.com", ...account },
  ...(proves
    ? {
        provenance: { version: 1, provider: "github", host: "github.com", viewer: account.login },
      }
    : {}),
  data,
});

function installShell() {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS, "review_threads", "review_thread_comments"],
          accounts: true,
          ...(proves ? { answerProvenance: 1 } : {}),
        },
      ],
      featureFlags: { names: [] },
    }),
    featureFlags: {
      subscribe: (_name: string, _options: unknown, listener: (value: unknown) => void) => {
        listener(undefined);
        return () => {};
      },
    },
    codeHostQuery: async (request: { operation: string; params: Record<string, unknown> }) => {
      if (request.operation === "account_list") {
        return { ok: true, data: [{ login: account.login, active: true, state: "success" }] };
      }
      const pending = deferred<ShellAnswer>();
      shell.reads.push({ ...request, answer: pending });
      return pending.promise;
    },
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: {
          intent: { mode: "automatic" },
          effective: { ...account },
          accounts: [],
        },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
  };
}

function pullRequest(url: string): CodeHostPullRequest {
  const [, , , owner, name, , number] = url.split("/");
  return {
    provider: "github",
    host: "github.com",
    repository: `${owner}/${name}`,
    number: Number(number),
    url,
  };
}

const threadsKey = (session: string) => [
  "github-review-threads",
  session,
  URL_ONE,
  "",
  REVISION,
  ...macReadScopeKey("github", "github.com"),
];

const comment = (id: string, body: string): ReviewComment =>
  ({ id, body, author: "carol", created_at: null, url: null }) as unknown as ReviewComment;

/** A thread with `comments`, and more of them on GitHub when `moreReplies`. */
function thread(id: string, comments: ReviewComment[], moreReplies?: string): ReviewThread {
  return {
    id,
    path: "src/app.ts",
    line: 3,
    is_resolved: false,
    comments: {
      object: "list",
      data: comments,
      has_more: !!moreReplies,
      next_cursor: moreReplies ?? null,
    },
  } as unknown as ReviewThread;
}

/** A page of `threads`, with more on GitHub when `moreThreads`. */
function page(threads: ReviewThread[], moreThreads?: string, warning?: string): ReviewThreadsPage {
  return {
    object: "list",
    data: threads,
    has_more: !!moreThreads,
    next_cursor: moreThreads ?? null,
    head_sha: HEAD,
    base_sha: BASE,
    ...(warning ? { warning } : {}),
  } as unknown as ReviewThreadsPage;
}

let unbindScopes: (() => void) | null = null;

/**
 * One app run in `session`: a fresh QueryClient and in-memory stores, as after a restart. The
 * view mounts once the shell said what it reads, as the page waits for that before reading.
 */
async function startRun(session = "session-1", hostAccount = "") {
  resetDesktopReadPathForTests();
  resetPullRequestDetailsCacheForTests();
  // Nothing in memory says who read before: only what's kept on this device does.
  resetMacReadScopesForTests();
  shell.reads = [];
  await desktopReadTarget(URL_ONE);
  // The page's lists have read as this Mac's account by the time a PR's conversations show.
  if (proves) {
    acceptMacRead(beginMacRead(macReadScopeKey("github", "github.com")), account.login);
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  unbindScopes?.();
  unbindScopes = bindMacReadScopes(client);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  // The account the session's host names, once its context answers; a Mac read names none.
  const view = renderHook(
    ({ named }: { named: string }) =>
      usePullRequestReviewThreads(session, URL_ONE, named, REVISION, true),
    { wrapper, initialProps: { named: hostAccount } },
  );
  return {
    client,
    view,
    /** The key the view reads under now: its session's own, or the PR's shared record. */
    get key() {
      const shown = client
        .getQueryCache()
        .findAll({ queryKey: ["github-review-threads"] })
        .find((query) => query.getObserversCount() > 0);
      return shown?.queryKey ?? threadsKey(session);
    },
    stop: () => {
      view.unmount();
      client.clear();
    },
  };
}
type Run = Awaited<ReturnType<typeof startRun>>;

/** Answer read number `index` (0-based) with `data`, once it was asked. */
async function answerRead(index: number, data: unknown) {
  await waitFor(() => expect(shell.reads.length).toBeGreaterThan(index));
  await act(async () => shell.reads[index]!.answer.resolve(answer(data)));
  await act(settle);
}

const settle = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 20);
  });

const shown = (run: Run) =>
  run.view.result.current.page!.data.map((value) => [
    value.id,
    value.comments.data.map((item) => item.body),
  ]);
const freshness = (run: Run) => run.view.result.current.freshness;
/** The age the shared mark shows, or null when it shows none. */
const mark = (run: Run) => pullRequestFreshness([freshness(run)])?.updatedAt ?? null;

const savedThreads = () =>
  Object.keys(window.localStorage)
    .filter((key) => key.startsWith(STORAGE_PREFIX) && !key.endsWith(":used"))
    .map((key) => JSON.parse(window.localStorage.getItem(key)!))
    .find((entry) => entry.state.data.kind === "threads");

/** Restart at `at`: the first page's read waits, and the kept page shows. */
async function restartAt(previous: Run, at: number, session = "session-2") {
  previous.stop();
  vi.setSystemTime(at);
  const run = await startRun(session);
  await waitFor(() => expect(run.view.result.current.page).toBeDefined());
  await waitFor(() => expect(shell.reads).toHaveLength(1));
  return run;
}

const T0 = Date.now() - 60 * MINUTE;
const T1 = T0 + 12 * MINUTE;
const T2 = T1 + 12 * MINUTE;

beforeEach(() => {
  vi.useFakeTimers({ now: T0, toFake: ["Date"] });
  window.localStorage.clear();
  githubMacReadsPreference.write("on");
  shell.reads = [];
  Object.assign(account, { login: "alice", generation: 1 });
  installShell();
});

afterEach(() => {
  vi.useRealTimers();
  githubCooldowns.reset();
  unbindScopes?.();
  unbindScopes = null;
  resetDesktopReadPathForTests();
  resetPullRequestDetailsCacheForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
  vi.restoreAllMocks();
});

describe.each([
  { proves: false, name: "keeps each session's conversations apart" },
  { proves: true, name: "shares one record of a PR's conversations" },
])("on a shell that $name", (variant) => {
  beforeEach(() => {
    proves = variant.proves;
  });
  afterEach(() => {
    proves = false;
  });

  // Only where the shell shares one record: the separate-record case is the same path.
  if (variant.proves) {
    describe("saved review conversations: writes", () => {
      it("W3: keeps an edited comment as old as its read, and its page's mark after a restart", async () => {
        const run = await startRun();
        await answerRead(0, page([thread("t1", [comment("c1", "root")])]));
        const after = await restartAt(run, T1);
        act(() => changeComment(after.client, after.key, "t1", "c1", "root, edited"));
        await act(settle);
        expect(shown(after)).toEqual([["t1", ["root, edited"]]]);
        expect(freshness(after)).toMatchObject({ updatedAt: T0, restored: true });
      });
    });
  }

  // Only where the shell shares one record: the separate-record case is the same path.
  if (variant.proves) {
    describe("saved review conversations: controls", () => {
      it("C1: a complete read again of everything shown clears the mark and takes its time", async () => {
        const run = await startRun();
        await answerRead(0, page([thread("t1", [comment("c1", "root")])]));
        const after = await restartAt(run, T1);
        expect(freshness(after)).toMatchObject({ updatedAt: T0, restored: true });
        await answerRead(0, page([thread("t1", [comment("c1", "root t1")])]));
        await act(settle);
        expect(freshness(after)).toMatchObject({ updatedAt: T1, restored: false });
        expect(mark(after)).toBeNull();
        expect(savedThreads().state.dataUpdatedAt).toBe(T1);
      });
    });
  }

  describe("saved review conversations: whose they are", () => {
    /** This Mac's account is `login` from now on, as after signing in as it while closed. */
    const signInAs = (login: string) => {
      Object.assign(account, { login, generation: account.generation + 1 });
    };
    const ownThread = (login: string) =>
      page([thread("t1", [comment("c1", `${login}'s view of the review`)])]);
    /** The run's first read is out and nothing kept was shown or is kept for another account. */
    async function nothingRestored(run: Run, other: string) {
      await waitFor(() => expect(shell.reads).toHaveLength(1));
      await act(settle);
      expect(run.view.result.current.page).toBeUndefined();
      expect(freshness(run).restored).toBe(false);
      expect(savedThreads()?.state.data.viewer ?? null).not.toBe(other);
    }

    // Only where the shell shares one record: the separate-record case is the same path.
    if (variant.proves) {
      it("A1: never shows Alice's kept conversations to Bob, nor Bob's to Alice on her return", async () => {
        const alice = await startRun("session-1");
        await answerRead(0, ownThread("alice"));
        expect(shown(alice)).toEqual([["t1", ["alice's view of the review"]]]);
        expect(savedThreads().state.data.viewer).toBe("alice");
        alice.stop();

        // Bob now, after a restart: Alice's conversations stay hidden, and Bob's are read.
        signInAs("bob");
        vi.setSystemTime(T1);
        const bob = await startRun("session-2");
        await nothingRestored(bob, "alice");
        // Bob's view reads his own record where the shell proves its reader, else its session's.
        expect(bob.key.includes("bob")).toBe(proves);
        await answerRead(0, ownThread("bob"));
        expect(shown(bob)).toEqual([["t1", ["bob's view of the review"]]]);
        expect(freshness(bob)).toMatchObject({ updatedAt: T1, restored: false });
        expect(savedThreads().state.data.viewer).toBe("bob");
        bob.stop();

        // Alice again: Bob's kept conversations stay hidden, and hers are read again.
        signInAs("alice");
        vi.setSystemTime(T2);
        const back = await startRun("session-1");
        await nothingRestored(back, "bob");
        await answerRead(0, ownThread("alice"));
        expect(shown(back)).toEqual([["t1", ["alice's view of the review"]]]);
        expect(freshness(back)).toMatchObject({ updatedAt: T2, restored: false });
      });
    }

    // Only where each session keeps its own: the shared-record case is the same path.
    if (!variant.proves) {
      it("A2: still shows Alice's kept conversations to Alice after a restart", async () => {
        const first = await startRun("session-1");
        await answerRead(0, ownThread("alice"));
        const again = await restartAt(first, T1);
        expect(shown(again)).toEqual([["t1", ["alice's view of the review"]]]);
        expect(freshness(again)).toMatchObject({ updatedAt: T0, restored: true });
      });
    }
  });
});
