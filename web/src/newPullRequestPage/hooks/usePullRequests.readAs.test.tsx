// A PR's files read inside the Pull Requests page as one of this Mac's logins, beside the same
// PR read as the host's account: each read names its own login, and neither answer shows in the other.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));
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

import {
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import {
  namedReadsAvailable,
  PULL_REQUESTS_PAGE_ACCOUNTS_FLAG,
  resetMacNamedReadsForTests,
} from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";

import { MacReadAsContext } from "../../hooks/macReadAs";
import { fetchPullRequestFileContents, usePullRequestChangedFiles } from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR = "https://github.com/example-org/universe/pull/42";
const BOB = { host: "github.com", login: "bob" };
const PAGE_AS_BOB = { reader: BOB };
const NAMED_KEY = ["mac-read-as", "user-1", "github", "github.com", "bob"];

/** What `login` sees of the PR: its files, or a file's contents, tagged with who read them. */
function dataFor(operation: string, login: string) {
  if (operation === "pull_request_files") {
    return {
      data: [{ path: `${login}.ts`, status: "modified", additions: 1, deletions: 0 }],
      has_more: false,
    };
  }
  return { before: null, after: `read as ${login}` };
}

/** A shell offering named reads behind the flag, reading github.com as `alice` otherwise. */
function installShell() {
  const flag: { set: ((value: unknown) => void) | null } = { set: null };
  const codeHostQuery = vi.fn(async (request: Record<string, unknown>) => {
    if (request.operation === "account_list") {
      return { ok: true, data: [{ login: "alice", active: true, state: "success" }] };
    }
    const login = typeof request.macLogin === "string" ? request.macLogin : "alice";
    return {
      ok: true,
      data: dataFor(String(request.operation), login),
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
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS],
          accounts: true,
          answerProvenance: 1,
          readAs: 1,
        },
      ],
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

/** Turn the flag on and settle where github.com reads, so the hooks read on this Mac. */
async function ready(shell: ReturnType<typeof installShell>) {
  githubMacReadsPreference.write("on");
  namedReadsAvailable();
  await vi.waitFor(() => expect(shell.flag.set).not.toBeNull());
  shell.flag.set!(true);
  await vi.waitFor(() => expect(namedReadsAvailable()).toBe(true));
  await desktopReadTarget(PR);
}

const requests = (shell: ReturnType<typeof installShell>, operation: string) =>
  shell.codeHostQuery.mock.calls
    .map(([request]) => request)
    .filter((request) => request.operation === operation);

const seen: { named?: string[]; hostWide?: string[] } = {};

function Files({ as }: { as: "named" | "hostWide" }) {
  seen[as] = usePullRequestChangedFiles("conv_1", true, PR).data?.data.map((file) => file.path);
  return null;
}

function renderBoth(client: QueryClient, { withNamed = true } = {}) {
  const page = (children: ReactNode) => (
    <MacReadAsContext.Provider value={PAGE_AS_BOB}>{children}</MacReadAsContext.Provider>
  );
  render(
    <QueryClientProvider client={client}>
      {withNamed && page(<Files as="named" />)}
      <Files as="hostWide" />
    </QueryClientProvider>,
  );
}

const changedFilesKeys = (client: QueryClient) =>
  client
    .getQueryCache()
    .findAll({ queryKey: ["github-changed-files"] })
    .map((query) => query.queryKey);

const containsInOrder = (key: readonly unknown[], part: readonly unknown[]) =>
  key.some((_, start) => part.every((value, offset) => key[start + offset] === value));

afterEach(() => {
  cleanup();
  seen.named = undefined;
  seen.hostWide = undefined;
  githubMacReadsPreference.write("automatic");
  resetMacNamedReadsForTests();
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("a PR's files read by the Pull Requests page as a named login", () => {
  it("names bob only inside the page, and keeps both answers apart in one cache", async () => {
    const shell = installShell();
    await ready(shell);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderBoth(client);

    await waitFor(() => expect(seen.named).toEqual(["bob.ts"]));
    await waitFor(() => expect(seen.hostWide).toEqual(["alice.ts"]));

    const reads = requests(shell, "pull_request_files");
    expect(reads).toHaveLength(2);
    expect(reads.filter((request) => request.macLogin === "bob")).toHaveLength(1);
    expect(reads.filter((request) => !("macLogin" in request))).toHaveLength(1);

    const keys = changedFilesKeys(client);
    expect(keys).toHaveLength(2);
    const named = keys.filter((key) => containsInOrder(key, NAMED_KEY));
    expect(named).toHaveLength(1);
    const hostWide = keys.find((key) => !named.includes(key))!;
    expect(hostWide).not.toContain("mac-read-as");
    expect(hostWide).not.toContain("bob");
    expect((client.getQueryData(named[0]!) as { data: { path: string }[] }).data[0]!.path).toBe(
      "bob.ts",
    );
    expect((client.getQueryData(hostWide) as { data: { path: string }[] }).data[0]!.path).toBe(
      "alice.ts",
    );
  });

  it("outside the page reads only as the host's account", async () => {
    const shell = installShell();
    await ready(shell);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderBoth(client, { withNamed: false });

    await waitFor(() => expect(seen.hostWide).toEqual(["alice.ts"]));
    const reads = requests(shell, "pull_request_files");
    expect(reads).toHaveLength(1);
    expect(reads[0]).not.toHaveProperty("macLogin");
    expect(changedFilesKeys(client).flat()).not.toContain("mac-read-as");
  });
});

describe("a file's contents loaded for the Pull Requests page", () => {
  const selected = { pr_url: PR, head_sha: "a".repeat(40), base_sha: "b".repeat(40) };

  it("reads as the page's login when given one, and as the host's account without", async () => {
    const shell = installShell();
    await ready(shell);

    const asBob = await fetchPullRequestFileContents(
      "conv_1",
      "src/a.ts",
      undefined,
      selected,
      BOB,
    );
    const asHost = await fetchPullRequestFileContents("conv_1", "src/a.ts", undefined, selected);

    const [named, hostWide] = requests(shell, "pull_request_file_contents");
    expect(named).toMatchObject({ account: "mac", macLogin: "bob" });
    expect(hostWide).toMatchObject({ account: "mac" });
    expect(hostWide).not.toHaveProperty("macLogin");
    expect(JSON.stringify(asBob)).toContain("read as bob");
    expect(JSON.stringify(asBob)).not.toContain("read as alice");
    expect(JSON.stringify(asHost)).toContain("read as alice");
    expect(JSON.stringify(asHost)).not.toContain("read as bob");
  });
});
