// A list row's read follows the same choices as any pull request's: a row this Mac found on the
// session's branch never reaches this Mac's reader where the user or the repository said not to.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: () => true,
  useSessionHostOnline: () => true,
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
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => "https://omnigent.example.com",
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

import {
  resetDesktopPullRequestWritesForTests,
  setMacWritesReleaseFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import {
  chooseReadSource,
  githubMacReadsPreference,
  readRouteFor,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { resetHostTransportRecovery } from "@/lib/hostTransportRecovery";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { resetRepoAccountsForTests, setRepoAccount } from "@/lib/pullRequestRepoAccounts";
import { resetRepoReaderForTests } from "@/lib/pullRequestRepoReader";
import { ReadSourceUnavailableError } from "@/lib/pullRequestReadRoute";
import {
  resetAcceptedWorkspaceDesignForTests,
  setAcceptedWorkspaceDesign,
} from "@/lib/workspaceSurfaces";
import { installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";

import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { usePullRequestListEntries } from "@/shell/usePullRequestListEntries";

import {
  type PullRequestAssociation,
  type PullRequestInfoRead,
  usePullRequestInfos,
} from "./usePullRequests";

const SESSION = "conv_parent";
// The branch's PR this Mac found; the session's stored links don't include it.
const FOUND = "https://github.com/acme/app/pull/42";
const APP = { host: "github.com", owner: "acme", repo: "app" };
const NOT_ASSOCIATED = "This pull request is not associated with the session";

function overview(login: string) {
  return {
    repository: "acme/app",
    pr: {
      number: 42,
      title: `Read on this Mac as ${login}`,
      state: "OPEN",
      url: FOUND,
      is_draft: false,
      author: "author",
      base_ref: "main",
      head_ref: "feature",
      head_sha: "a".repeat(40),
      base_sha: "b".repeat(40),
      body: null,
      checks: { passing: 0, failing: 0, pending: 0, cancelled: 0, skipped: 0, total: 0, runs: [] },
      comments: [],
      reviewers: [],
      labels: [],
    },
  };
}

type Shell = ReturnType<typeof installNamedWriteShell>;

/** A shell reading github.com as `alice`, and by name as `bob`, with the branch's PR known. */
async function readyShell() {
  const shell = installNamedWriteShell(undefined, {
    reads: { pull_request_overview: (login) => overview(login) },
  });
  await shell.ready(FOUND, { asBob: false });
  shell.codeHostQuery.mockClear();
  return shell;
}

/** Who each of this Mac's overview reads went out as: a login by name, or `default` for gh's active account. */
function macOverviewsAs(shell: Shell): string[] {
  return shell.codeHostQuery.mock.calls
    .map(([request]) => request)
    .filter((request) => request.operation === "pull_request_overview")
    .map((request) => (typeof request.macLogin === "string" ? request.macLogin : "default"));
}

/** The server: the session's machine reads only what the session links, which isn't the row. */
function stubServer() {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const prUrl = new URL(String(input), "http://localhost").searchParams.get("pr_url");
      if (prUrl) asked.push(prUrl);
      return new Response(JSON.stringify({ detail: NOT_ASSOCIATED }), { status: 400 });
    }),
  );
  return asked;
}

const seen: { reads?: PullRequestInfoRead[] } = {};

function Rows() {
  seen.reads = usePullRequestInfos(SESSION, [FOUND], { poll: false });
  return null;
}

async function readRow(): Promise<PullRequestInfoRead> {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Rows />
    </QueryClientProvider>,
  );
  await waitFor(() => {
    const read = seen.reads?.[0];
    expect(read?.data?.pr ?? read?.error).toBeTruthy();
  });
  return seen.reads![0]!;
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  seen.reads = undefined;
  vi.unstubAllGlobals();
  githubMacReadsPreference.write("automatic");
  setMacWritesReleaseFlagForTests(null);
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  resetRepoReaderForTests();
  resetRepoAccountsForTests();
  resetAcceptedWorkspaceDesignForTests();
  resetHostTransportRecovery();
  removeNamedWriteShell();
  window.localStorage.clear();
});

describe("a row this Mac found on the branch", () => {
  it("reads on this Mac as the host's account on Automatic", async () => {
    const shell = await readyShell();
    const asked = stubServer();

    const read = await readRow();

    expect(read.data?.pr?.title).toBe("Read on this Mac as alice");
    expect(macOverviewsAs(shell)).toEqual(["default"]);
    expect(asked).toEqual([]);
  });

  it.each([
    ["the PR set to Session's machine", () => chooseReadSource(FOUND, "session")],
    ["Mac reads turned off", () => githubMacReadsPreference.write("off")],
    [
      "This Mac chosen on the classic page, which reads through the session's machine",
      () => {
        setAcceptedWorkspaceDesign("classic");
        chooseReadSource(FOUND, "mac");
      },
    ],
  ])("goes to the session's machine with %s, never this Mac's reader", async (_case, choose) => {
    const shell = await readyShell();
    const asked = stubServer();
    choose();
    expect(readRouteFor(FOUND)).toEqual({ kind: "host" });

    const read = await readRow();

    expect(read.error?.message).toContain(NOT_ASSOCIATED);
    expect(asked).toContain(FOUND);
    expect(macOverviewsAs(shell)).toEqual([]);
  });

  it("stays blocked where its repository's chosen login can't be used, asking nobody", async () => {
    const shell = await readyShell();
    const asked = stubServer();
    expect(setRepoAccount(APP, { mode: "account", login: "bob" })).toBe(true);
    await shell.loseNamedSupport();
    expect(readRouteFor(FOUND)).toEqual({ kind: "blocked", reason: "repo_account" });
    shell.codeHostQuery.mockClear();

    const read = await readRow();

    expect(read.error).toBeInstanceOf(ReadSourceUnavailableError);
    expect(asked).toEqual([]);
    expect(macOverviewsAs(shell)).toEqual([]);
  });

  it("reads only as its repository's chosen login, never as the host's account or on the session's machine", async () => {
    const shell = await readyShell();
    const asked = stubServer();
    expect(setRepoAccount(APP, { mode: "account", login: "bob" })).toBe(true);

    const read = await readRow();

    expect(read.data?.pr?.title).toBe("Read on this Mac as bob");
    expect(macOverviewsAs(shell)).toEqual(["bob"]);
    expect(asked).toEqual([]);
  });
});

it("makes no server write for a row this Mac read: nothing stored, linked or sent to GitHub", async () => {
  const shell = await readyShell();
  const requests: { method: string; url: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ method: (init?.method ?? "GET").toUpperCase(), url: String(input) });
      return new Response(JSON.stringify({ detail: NOT_ASSOCIATED }), { status: 400 });
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <SidebarDataProvider>{children}</SidebarDataProvider>
    </QueryClientProvider>
  );
  const found = {
    url: FOUND,
    host: "github.com",
    repository: "acme/app",
    number: 42,
    relationship: "inferred",
    found_on_mac: true,
  } as PullRequestAssociation;
  // The owner's view, where the row offers Link to This Session until they choose it.
  const { result } = renderHook(
    () =>
      usePullRequestListEntries({
        conversationId: SESSION,
        active: true,
        associations: [found],
        ownsSessionTree: true,
        hostOffline: false,
        sandboxAsleep: false,
        onSelect: vi.fn(),
        onUnlink: vi.fn(),
        unlinkPending: false,
      }),
    { wrapper },
  );

  await waitFor(() =>
    expect(result.current.entries[0]?.pr?.title).toBe("Read on this Mac as alice"),
  );

  expect(requests.filter((request) => request.method !== "GET")).toEqual([]);
  expect(requests.filter((request) => request.url.includes("pr_url="))).toEqual([]);
  expect(shell.codeHostWrite).not.toHaveBeenCalled();
  expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
});
