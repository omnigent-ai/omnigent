// Selecting the PR the session's own read already shows keeps it on screen while
// the PR's own read loads, and only then: never across sessions, PRs or read paths.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
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
import {
  type PullRequestInfo,
  sessionReadOfSelectedPullRequest,
  useDesktopPullRequestReads,
  usePullRequestInfo,
} from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const SELECTED = "https://github.com/acme/app/pull/7";
const OTHER = "https://github.com/acme/app/pull/8";

function pullRequest(url: string, title: string) {
  return {
    number: Number(url.split("/").pop()),
    title,
    state: "OPEN",
    url,
    is_draft: false,
    author: "octocat",
    base_ref: "main",
    head_ref: "feature",
    checks: { passing: 0, failing: 0, pending: 0, cancelled: 0, skipped: 0, total: 0, runs: [] },
    comments: [],
    reviewers: [],
  };
}

function sessionInfo(prUrl: string, title: string) {
  return {
    object: "session.github.info",
    available: true,
    provider: "github",
    tracking_available: true,
    repo: { name_with_owner: "acme/app" },
    prs: [{ url: SELECTED, host: "github.com", repository: "acme/app", number: 7 }],
    selected_pr_url: prUrl,
    pr: pullRequest(prUrl, title),
  };
}

/** The session's machine answers its own read at once and holds every PR's own read. */
function stubHost() {
  const held: (() => void)[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const prUrl = new URL(String(input), "http://localhost").searchParams.get("pr_url");
      if (!prUrl) return new Response(JSON.stringify(sessionInfo(SELECTED, "Session read")));
      await new Promise<void>((resolve) => {
        held.push(resolve);
      });
      return new Response(JSON.stringify(sessionInfo(prUrl, "PR read")));
    }),
  );
  return { pending: () => held.length, release: () => held.splice(0).forEach((go) => go()) };
}

/** A desktop shell that reads github.com on this Mac and never answers the read. */
function installMacReadingShell({ featuresPending = false } = {}) {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async (request: { operation: string }) =>
      request.operation === "account_list"
        ? { ok: true, data: [{ login: "mac-octo", active: true, state: "success" }] }
        : new Promise(() => {}),
    ),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: featuresPending
      ? () => new Promise(() => {})
      : async () => ({
          codeHosts: [
            { id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS], accounts: true },
          ],
        }),
  };
}

let seen: {
  info?: PullRequestInfo;
  placeholder?: boolean;
  known?: boolean;
  onMac?: boolean;
} = {};

function Probe({ sessionId, prUrl }: { sessionId: string; prUrl?: string }) {
  const info = usePullRequestInfo(sessionId, { prUrl });
  const reads = useDesktopPullRequestReads();
  seen.info = info.data;
  seen.placeholder = info.isPlaceholderData;
  seen.known = reads.known(SELECTED);
  seen.onMac = reads.readsOnThisMac(SELECTED);
  return null;
}

function renderProbe() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <Probe sessionId="conv_1" />
    </QueryClientProvider>,
  );
  const show = (sessionId: string, prUrl?: string) =>
    view.rerender(
      <QueryClientProvider client={client}>
        <Probe sessionId={sessionId} prUrl={prUrl} />
      </QueryClientProvider>,
    );
  return show;
}

beforeEach(() => {
  seen = {};
  resetHostTransportRecovery();
});

afterEach(() => {
  cleanup();
  githubMacReadsPreference.write("automatic");
  resetDesktopReadPathForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  vi.unstubAllGlobals();
});

describe("selecting the PR the session's read already shows", () => {
  it("keeps the session's read on screen until the PR's own read answers", async () => {
    const host = stubHost();
    const show = renderProbe();
    await waitFor(() => expect(seen.info?.pr?.title).toBe("Session read"));

    show("conv_1", SELECTED);
    expect(seen.info?.pr?.title).toBe("Session read");
    expect(seen.placeholder).toBe(true);

    await waitFor(() => expect(host.pending()).toBe(1));
    act(host.release);
    await waitFor(() => expect(seen.info?.pr?.title).toBe("PR read"));
    expect(seen.placeholder).toBe(false);
  });

  it("shows nothing borrowed for a PR the session's read doesn't show", async () => {
    stubHost();
    const show = renderProbe();
    await waitFor(() => expect(seen.info?.pr?.title).toBe("Session read"));

    show("conv_1", OTHER);
    expect(seen.info).toBeUndefined();
  });

  it("shows nothing borrowed from another session", async () => {
    stubHost();
    const show = renderProbe();
    await waitFor(() => expect(seen.info?.pr?.title).toBe("Session read"));

    show("conv_2", SELECTED);
    expect(seen.info).toBeUndefined();
  });

  it("shows nothing borrowed when this Mac reads the PR as its own account", async () => {
    stubHost();
    installMacReadingShell();
    githubMacReadsPreference.write("on");
    const show = renderProbe();
    await waitFor(() => expect(seen.info?.pr?.title).toBe("Session read"));
    await waitFor(() => expect(seen.onMac).toBe(true));

    show("conv_1", SELECTED);
    expect(seen.info?.pr?.title).not.toBe("Session read");
  });

  it("shows nothing borrowed while the shell hasn't said where the PR reads", async () => {
    stubHost();
    installMacReadingShell({ featuresPending: true });
    const show = renderProbe();
    await waitFor(() => expect(seen.info?.pr?.title).toBe("Session read"));
    expect(seen.known).toBe(false);

    show("conv_1", SELECTED);
    expect(seen.info).toBeUndefined();
  });
});

describe("sessionReadOfSelectedPullRequest", () => {
  const read = sessionInfo(SELECTED, "Session read") as unknown as PullRequestInfo;

  it("lends the session-wide read for its own selected PR on the host path", () => {
    expect(sessionReadOfSelectedPullRequest(read, ["github-info", "c"], "c", SELECTED, true)).toBe(
      read,
    );
  });

  it.each([
    ["no previous read", undefined, ["github-info", "c"], "c", SELECTED, true],
    ["a PR's own previous read", read, ["github-info", "c", SELECTED], "c", SELECTED, true],
    ["another session's read", read, ["github-info", "other"], "c", SELECTED, true],
    ["a different PR", read, ["github-info", "c"], "c", OTHER, true],
    ["the session-wide read itself", read, ["github-info", "c"], "c", undefined, true],
    ["a read that doesn't go to the host", read, ["github-info", "c"], "c", SELECTED, false],
  ] as const)("lends nothing for %s", (_name, previous, key, sessionId, prUrl, fromHost) => {
    expect(
      sessionReadOfSelectedPullRequest(previous, key, sessionId, prUrl, fromHost),
    ).toBeUndefined();
  });

  it("lends nothing when the read selects the PR but describes another", () => {
    const mismatched = { ...read, pr: { ...read.pr!, url: OTHER } } as PullRequestInfo;
    expect(
      sessionReadOfSelectedPullRequest(mismatched, ["github-info", "c"], "c", SELECTED, true),
    ).toBeUndefined();
  });
});
