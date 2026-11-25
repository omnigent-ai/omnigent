// A PR in a session-free view, such as the Pull Requests page showing one no session
// links: it reads on this Mac and asks nothing about the session its ID would name.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type * as CodeHostContextModule from "@/lib/codeHostContext";
import type * as LocalLinksModule from "@/lib/localPullRequestLinks";

// Even with a host that reads as online, the view's ID has no session behind it.
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
vi.mock("@/lib/localPullRequestLinks", async (importOriginal) => ({
  ...(await importOriginal<typeof LocalLinksModule>()),
  listLocalPullRequests: vi.fn(async () => []),
}));
vi.mock("@/lib/codeHostContext", async (importOriginal) => ({
  ...(await importOriginal<typeof CodeHostContextModule>()),
  useCodeHostContext: vi.fn(() => ({ data: undefined })),
}));

import { useCodeHostContext } from "@/lib/codeHostContext";
import { GITHUB_MAC_READ_OPERATIONS, githubMacReadsPreference } from "@/lib/desktopReadPath";
import { listLocalPullRequests } from "@/lib/localPullRequestLinks";

import { MacPullRequestScopeContext } from "../../hooks/macPullRequestScope";
import { type PullRequestInfo, usePullRequestInfo } from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const SCOPE = "pull-requests-page";
const PR_URL = "https://github.com/acme/widgets/pull/42";

function overview() {
  return {
    repository: "acme/widgets",
    pr: {
      number: 42,
      title: "Read on this Mac",
      state: "OPEN",
      url: PR_URL,
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

/** A shell signed in to GitHub as `mac-octo`, answering every read on this Mac. */
function installShell() {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async (request: { operation: string; host: string }) =>
      request.operation === "account_list"
        ? { ok: true, data: [{ login: "mac-octo", active: true, state: "success" }] }
        : {
            ok: true,
            data: overview(),
            identity: { host: request.host, login: "mac-octo", generation: 1 },
          },
    ),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [{ id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS], accounts: true }],
    }),
  };
}

const seen: { info?: PullRequestInfo } = {};

function Probe({ conversationId }: { conversationId: string }) {
  seen.info = usePullRequestInfo(conversationId, { prUrl: PR_URL, findBranch: true }).data;
  return null;
}

function renderInScope(conversationId: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MacPullRequestScopeContext.Provider value={SCOPE}>
        <Probe conversationId={conversationId} />
      </MacPullRequestScopeContext.Provider>
    </QueryClientProvider>,
  );
}

let hostFetch: ReturnType<typeof vi.fn>;

beforeEach(() => {
  seen.info = undefined;
  installShell();
  githubMacReadsPreference.write("on");
  hostFetch = vi.fn(async () => new Response("{}", { status: 500 }));
  vi.stubGlobal("fetch", hostFetch);
});

afterEach(() => {
  cleanup();
  githubMacReadsPreference.write("automatic");
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  vi.unstubAllGlobals();
  vi.mocked(listLocalPullRequests).mockClear();
  vi.mocked(useCodeHostContext).mockClear();
});

it("reads the PR on this Mac without asking about a session, its links or its branch", async () => {
  renderInScope(SCOPE);
  await waitFor(() => expect(seen.info?.pr?.title).toBe("Read on this Mac"));
  expect(seen.info?.read_on_mac?.login).toBe("mac-octo");
  expect(hostFetch.mock.calls.map(([url]) => String(url))).toEqual([]);
  expect(listLocalPullRequests).not.toHaveBeenCalled();
  for (const [, options] of vi.mocked(useCodeHostContext).mock.calls) {
    expect(options?.enabled).toBe(false);
  }
});

it("still looks for a real session's branch PR inside the view", async () => {
  renderInScope("conv_1");
  await waitFor(() => expect(seen.info?.pr?.title).toBe("Read on this Mac"));
  expect(vi.mocked(useCodeHostContext)).toHaveBeenCalledWith(
    "conv_1",
    expect.objectContaining({ enabled: true }),
  );
});
