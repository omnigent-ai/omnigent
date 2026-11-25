// A PR's commits and one commit's patch, read on this Mac when the shell offers them: keyed by the
// read path and account they came from, never a host answer for a PR this Mac reads.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { type ReactNode, useMemo } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

const liveness = vi.hoisted(() => ({
  runner: true as boolean | undefined,
  host: true as boolean | undefined,
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: () => liveness.runner,
  useSessionHostOnline: () => liveness.host,
}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: unknown) =>
    typeof selector === "function"
      ? (selector as (state: { conversationId: null; sessionStatus: "idle" }) => unknown)({
          conversationId: null,
          sessionStatus: "idle",
        })
      : undefined,
}));

import {
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { DesktopPullRequestError } from "@/lib/desktopPullRequests";
import {
  namedReadsAvailable,
  PULL_REQUESTS_PAGE_ACCOUNTS_FLAG,
  resetMacNamedReadsForTests,
} from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import type { MacNamedReader } from "@/lib/macNamedReads";

import { MacReadAsContext } from "../../hooks/macReadAs";
import {
  usePullRequestCommitDiff,
  usePullRequestCommits,
  usePullRequestCommitsUnsupported,
} from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR = "https://github.com/example-org/example-repo/pull/42";
const HEAD = "a".repeat(40);
const BASE = "b".repeat(40);
const SHA_A = "1".repeat(40);
const COMMIT_OPERATIONS = ["pull_request_commits", "commit_diff"];

type Answer = { ok: false; error: { code: string; message?: string } } | Record<string, unknown>;

/** A shell offering the Mac reads of one PR; `answer` gives each operation's reply for a login. */
function installShell({
  operations = [...GITHUB_MAC_READ_OPERATIONS, ...COMMIT_OPERATIONS],
  githubHost = true,
  answer,
}: {
  operations?: string[];
  githubHost?: boolean;
  answer: (request: Record<string, unknown>, login: string) => Answer | Promise<Answer>;
}) {
  const flag: { set: ((value: unknown) => void) | null } = { set: null };
  const codeHostQuery = vi.fn(async (request: Record<string, unknown>) => {
    if (request.operation === "account_list") {
      return { ok: true, data: [{ login: "alice", active: true, state: "success" }] };
    }
    const login = typeof request.macLogin === "string" ? request.macLogin : "alice";
    const result = await answer(request, login);
    if ("ok" in result) return result;
    return {
      ok: true,
      data: result,
      identity: request.macLogin
        ? { host: "github.com", login, generation: 0, named: true }
        : { host: "github.com", login, generation: 1 },
      provenance: { version: 1, provider: "github", host: "github.com", viewer: login },
    };
  });
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery,
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
      onNamedChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: githubHost
        ? [
            {
              id: "github",
              operations,
              accounts: true,
              answerProvenance: 1,
              readAs: 1,
            },
          ]
        : [],
      featureFlags: { names: [PULL_REQUESTS_PAGE_ACCOUNTS_FLAG] },
    }),
    featureFlags: {
      subscribe: (name: string, _options: unknown, callback: (value: unknown) => void) => {
        if (name === PULL_REQUESTS_PAGE_ACCOUNTS_FLAG) flag.set = callback;
        return () => {};
      },
    },
  };
  return { codeHostQuery, flag };
}

/** The shell's commit list at the head `answer` names, with one commit by `login`. */
function commitList(login: string, headSha: string | null = HEAD, complete = true) {
  return {
    head_sha: headSha,
    base_sha: BASE,
    total_count: 1,
    complete,
    commits: [
      {
        sha: SHA_A,
        subject: `Add a as ${login}`,
        author_name: login,
        authored_at: "2026-01-02T00:00:00Z",
        committed_at: null,
        author_login: login,
        url: null,
        ci_state: null,
      },
    ],
  };
}

const commitPatch = (login: string) => ({ sha: SHA_A, patch: `diff as ${login}\n` });

/** Turns on this Mac's GitHub reads and settles the shell, as the Pull Requests page would. */
async function ready(shell: ReturnType<typeof installShell>, { named = false } = {}) {
  githubMacReadsPreference.write("on");
  if (named) {
    namedReadsAvailable();
    await vi.waitFor(() => expect(shell.flag.set).not.toBeNull());
    shell.flag.set!(true);
    await vi.waitFor(() => expect(namedReadsAvailable()).toBe(true));
  }
  await desktopReadTarget(PR);
}

/** The Pull Requests page's named reader, memoized so its value holds across renders. */
function PageReadAs({ reader, children }: { reader: MacNamedReader | null; children: ReactNode }) {
  const value = useMemo(() => ({ reader }), [reader]);
  return <MacReadAsContext.Provider value={value}>{children}</MacReadAsContext.Provider>;
}

/** Lists the commits, the Commits view's unsupported state, and one commit's patch, for a page. */
function renderCommits(
  client: QueryClient,
  page: { reader: MacNamedReader | null } = { reader: null },
) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <PageReadAs reader={page.reader}>{children}</PageReadAs>
    </QueryClientProvider>
  );
  return renderHook(
    () => ({
      list: usePullRequestCommits("conv_1", PR, HEAD, true),
      unsupported: usePullRequestCommitsUnsupported("conv_1", PR),
      diff: usePullRequestCommitDiff("conv_1", PR, SHA_A),
    }),
    { wrapper },
  );
}

const newClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

const fetchMock = vi.fn();

beforeEach(() => {
  liveness.runner = true;
  liveness.host = true;
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({}) } as Response);
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  githubMacReadsPreference.write("automatic");
  resetMacNamedReadsForTests();
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
  vi.unstubAllGlobals();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("a PR's commits read on this Mac", () => {
  it("lists the commits and one commit's patch on a capable shell, with no host request", async () => {
    const shell = installShell({
      answer: (request, login) =>
        request.operation === "pull_request_commits" ? commitList(login) : commitPatch(login),
    });
    await ready(shell);
    const { result } = renderCommits(newClient());

    await waitFor(() => expect(result.current.list.data?.data).toHaveLength(1));
    expect(result.current.list.data).toEqual({
      object: "list",
      has_more: false,
      data: [
        {
          object: "session.github.commit",
          sha: SHA_A,
          title: "Add a as alice",
          author: "alice",
          date: "2026-01-02T00:00:00Z",
        },
      ],
    });
    await waitFor(() => expect(result.current.diff.data?.patch).toBe("diff as alice\n"));
    expect(result.current.diff.data).toEqual({
      object: "session.github.pr_diff",
      patch: "diff as alice\n",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("stays enabled while the session's host is offline, and reads on this Mac", async () => {
    liveness.runner = false;
    liveness.host = false;
    const shell = installShell({
      answer: (request, login) =>
        request.operation === "pull_request_commits" ? commitList(login) : commitPatch(login),
    });
    await ready(shell);
    const { result } = renderCommits(newClient());

    await waitFor(() => expect(result.current.list.data?.data).toHaveLength(1));
    await waitFor(() => expect(result.current.diff.data?.patch).toBe("diff as alice\n"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("stays disabled while the session's host is offline and this Mac doesn't read the PR", async () => {
    liveness.runner = false;
    liveness.host = false;
    const shell = installShell({ answer: (_request, login) => commitList(login) });
    githubMacReadsPreference.write("off");
    await desktopReadTarget(PR);
    shell.codeHostQuery.mockClear();
    const { result } = renderCommits(newClient());

    expect(result.current.list.fetchStatus).toBe("idle");
    expect(result.current.diff.fetchStatus).toBe("idle");
    expect(shell.codeHostQuery).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not cache a list at another head under the head the panel shows", async () => {
    const moved = "c".repeat(40);
    const shell = installShell({
      answer: (request, login) =>
        request.operation === "pull_request_commits"
          ? commitList(login, moved)
          : commitPatch(login),
    });
    await ready(shell);
    const client = newClient();
    const { result } = renderCommits(client);

    await waitFor(() => expect(result.current.list.error).toBeInstanceOf(DesktopPullRequestError));
    expect(result.current.list.error).toMatchObject({ code: "pull_request_changed" });
    expect(result.current.list.data).toBeUndefined();
    for (const query of client.getQueryCache().findAll({ queryKey: ["github-pr-commits"] })) {
      expect(query.state.data).toBeUndefined();
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
