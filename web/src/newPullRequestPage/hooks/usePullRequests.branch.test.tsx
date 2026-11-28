// The session branch's PR listed by the session's host (ListBranchPullRequests), in place
// of the one the session info inferred; without the host's method the info stands.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as PullRequestRpcModule from "@/lib/pullRequestRpc";

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
const host = vi.hoisted(() => ({
  serves: true,
  listBranch: vi.fn<(branch: string) => Promise<unknown>>(),
}));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (_sessionId: string, method: string) =>
    host.serves && method === "listBranchPullRequests",
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => {
  const actual = (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(
    await importOriginal<typeof PullRequestRpcModule>(),
  );
  return {
    ...actual,
    readPullRequestRpc: (async (conversationId, method, read, signal) => {
      if (method !== "listBranchPullRequests") {
        return actual.readPullRequestRpc(conversationId, method, read, signal);
      }
      let branch = "";
      await read({
        listBranchPullRequests: async (request: { branch: string }) => {
          branch = request.branch;
          return {};
        },
      } as never);
      return host.listBranch(branch);
    }) as typeof actual.readPullRequestRpc,
  };
});

import { chooseBranchPullRequest, resetBranchChoicesForTests } from "@/lib/branchPullRequest";
import { resetHostTransportRecovery } from "@/lib/hostTransportRecovery";
import { type PullRequestInfo, usePullRequestInfo } from "./usePullRequests";

const FOUND = "https://github.com/acme/app/pull/7";
const HOST_INFERRED = "https://github.com/acme/app/pull/99";

const row = (url: string, number: number) => ({
  url,
  number,
  title: `PR ${number}`,
  state: "OPEN",
  is_draft: false,
  updated_at: "2026-01-01T00:00:00Z",
  head_ref: "feature",
});

/** The session's info: on `branch`, listing the PR it inferred from it. */
function stubSessionInfo(branch = "feature") {
  const info = {
    object: "session.github.info",
    available: true,
    provider: "github",
    tracking_available: true,
    branch,
    repo: { name_with_owner: "acme/app" },
    prs: [
      {
        url: HOST_INFERRED,
        host: "github.com",
        repository: "acme/app",
        number: 99,
        relationship: "inferred",
      },
    ],
    selected_pr_url: HOST_INFERRED,
    pr: null,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(info))),
  );
}

const seen: { info?: PullRequestInfo } = {};

function Probe({ findBranch = true }: { findBranch?: boolean }) {
  seen.info = usePullRequestInfo("conv_1", { findBranch }).data;
  return null;
}

function renderProbe(props: { findBranch?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Probe {...props} />
    </QueryClientProvider>,
  );
  return client;
}

const urls = () => seen.info?.prs?.map((pr) => pr.url);

beforeEach(() => {
  seen.info = undefined;
  host.serves = true;
  host.listBranch.mockReset();
  host.listBranch.mockResolvedValue({
    object: "list",
    data: [row(FOUND, 7)],
    has_more: false,
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  resetBranchChoicesForTests();
  resetHostTransportRecovery();
});

describe("where the host lists a branch's pull requests", () => {
  it("asks for the session's branch, and its PR replaces the inferred one, selected", async () => {
    stubSessionInfo();
    renderProbe();
    await waitFor(() => expect(urls()).toEqual([FOUND]));
    expect(host.listBranch).toHaveBeenCalledWith("feature");
    expect(seen.info?.selected_pr_url).toBe(FOUND);
    expect(seen.info?.prs?.[0]).toMatchObject({
      relationship: "inferred",
      provider: "github",
      found_for_branch: true,
    });
    expect(seen.info?.branch_discovery).toBeUndefined();
  });

  it("asks which PR when it lists several, and applies the choice", async () => {
    host.listBranch.mockResolvedValue({
      data: [row(FOUND, 7), row(`${FOUND}0`, 70)],
      has_more: false,
    });
    stubSessionInfo();
    renderProbe();
    await waitFor(() => expect(seen.info?.branch_discovery?.status).toBe("ambiguous"));
    expect(urls()).toEqual([]);
    act(() => chooseBranchPullRequest({ sessionId: "conv_1", branch: "feature" }, { url: FOUND }));
    await waitFor(() => expect(urls()).toEqual([FOUND]));
    expect(seen.info?.branch_discovery).toBeUndefined();
  });

  it("shows a failed lookup's notice and keeps the session info", async () => {
    host.listBranch.mockRejectedValue(
      new Error("GitHub couldn't list this branch's pull requests."),
    );
    stubSessionInfo();
    renderProbe();
    await waitFor(() => expect(seen.info?.branch_discovery?.status).toBe("failed"));
    expect(urls()).toEqual([HOST_INFERRED]);
  });

  it("isn't asked on a detached HEAD", async () => {
    stubSessionInfo("HEAD");
    renderProbe();
    await waitFor(() => expect(urls()).toEqual([HOST_INFERRED]));
    expect(host.listBranch).not.toHaveBeenCalled();
  });

  it("isn't asked by callers that don't, such as sidebar rows", async () => {
    stubSessionInfo();
    renderProbe({ findBranch: false });
    await waitFor(() => expect(urls()).toEqual([HOST_INFERRED]));
    expect(host.listBranch).not.toHaveBeenCalled();
  });
});

describe("where the host doesn't list a branch's pull requests", () => {
  it("shows the session info as it is, with no notice", async () => {
    host.serves = false;
    stubSessionInfo();
    renderProbe();
    await waitFor(() => expect(urls()).toEqual([HOST_INFERRED]));
    expect(seen.info?.branch_discovery).toBeUndefined();
    expect(seen.info?.branch_looked_up).toBeUndefined();
    expect(host.listBranch).not.toHaveBeenCalled();
  });
});
