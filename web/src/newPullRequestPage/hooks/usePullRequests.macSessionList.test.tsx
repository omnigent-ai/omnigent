// A PR this Mac reads lists the session's linked PRs from the session's own read, and says the
// session tracks them even once none are linked, so its stored and sub-agent PRs still list.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { resetHostTransportRecovery } from "@/lib/hostTransportRecovery";
import { type PullRequestInfo, usePullRequestInfo } from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const SHOWN = "https://github.com/acme/app/pull/7";
const LINKED = "https://github.com/acme/app/pull/8";

const pullRequest = (url: string) => ({
  number: Number(url.split("/").pop()),
  title: "Read on this Mac",
  state: "MERGED",
  url,
  is_draft: false,
  author: "octocat",
  base_ref: "main",
  head_ref: "feature",
  head_sha: "a".repeat(40),
  base_sha: "b".repeat(40),
  checks: { passing: 0, failing: 0, pending: 0, cancelled: 0, skipped: 0, total: 0, runs: [] },
  comments: [],
  reviewers: [],
});

/** The session's machine tracks its PRs and lists `linked`; it never reads the shown PR itself. */
function stubSessionHost(linked: string[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const prUrl = new URL(String(input), "http://localhost").searchParams.get("pr_url");
      if (prUrl) return new Promise<Response>(() => {});
      return new Response(
        JSON.stringify({
          object: "session.github.info",
          available: true,
          provider: "github",
          tracking_available: true,
          repo: { name_with_owner: "acme/app" },
          prs: linked.map((url) => ({
            url,
            host: "github.com",
            repository: "acme/app",
            number: Number(url.split("/").pop()),
          })),
          pr: null,
        }),
      );
    }),
  );
}

/** A desktop shell that reads github.com PRs on this Mac and answers their overview. */
function installMacReadingShell() {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async (request: { operation: string; number?: number }) =>
      request.operation === "account_list"
        ? { ok: true, data: [{ login: "mac-octo", active: true, state: "success" }] }
        : request.operation === "pull_request_overview"
          ? { ok: true, data: { repository: "acme/app", pr: pullRequest(SHOWN) } }
          : { ok: false, error: { code: "unsupported", message: "Not in this test." } },
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
  githubMacReadsPreference.write("on");
}

let seen: PullRequestInfo | undefined;

function Probe() {
  seen = usePullRequestInfo("conv_1", { prUrl: SHOWN }).data;
  return null;
}

function renderProbe() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Probe />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  seen = undefined;
  resetHostTransportRecovery();
});

afterEach(() => {
  cleanup();
  githubMacReadsPreference.write("automatic");
  resetDesktopReadPathForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  vi.unstubAllGlobals();
});

describe("a PR read on this Mac, beside the session's linked PRs", () => {
  it("lists the session's linked PRs and says it tracks them", async () => {
    stubSessionHost([LINKED]);
    installMacReadingShell();
    renderProbe();
    await waitFor(() => expect(seen?.prs?.map((pr) => pr.url)).toEqual([LINKED]));
    expect(seen?.tracking_available).toBe(true);
    expect(seen?.pr?.title).toBe("Read on this Mac");
  });

  it("still says the session tracks its PRs once all of them were removed", async () => {
    stubSessionHost([]);
    installMacReadingShell();
    renderProbe();
    await waitFor(() => expect(seen?.pr?.title).toBe("Read on this Mac"));
    // Without it, the list beside this PR ignores the session's stored and sub-agent PRs.
    await waitFor(() => expect(seen?.tracking_available).toBe(true));
    expect(seen?.prs).toEqual([]);
  });
});
