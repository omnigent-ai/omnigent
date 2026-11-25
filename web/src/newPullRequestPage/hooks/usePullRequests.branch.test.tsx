// The session branch's PR found on this Mac, with the real read-path store and a
// shell that serves the repository context and branch discovery.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const health = vi.hoisted(() => ({ online: true }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: () => health.online,
  useSessionHostOnline: () => health.online,
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

import { resetCodeHostContextReads } from "@/lib/codeHostContext";
import {
  findBranchOnSessionMachine,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { resetHostTransportRecovery } from "@/lib/hostTransportRecovery";
import { resetWorkspaceFastTrackForTests } from "@/lib/workspaceFastTrack";
import { type PullRequestInfo, usePullRequestInfo } from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const HEAD = "a".repeat(40);
const MAC_PR = "https://github.com/acme/app/pull/7";
const HOST_INFERRED = "https://github.com/acme/app/pull/99";

function context(overrides: Record<string, unknown> = {}) {
  return {
    status: "ok",
    context_key: "key-a",
    branch: "feature",
    head_sha: HEAD,
    push: {
      remote_name: "origin",
      branch: "feature",
      host: "github.com",
      owner: "acme",
      repo: "app",
      tracking_sha: HEAD,
    },
    push_unresolved: null,
    remotes: [{ name: "origin", host: "github.com", owner: "acme", repo: "app" }],
    unsupported_remote_count: 0,
    truncated_remotes: false,
    ...overrides,
  };
}

const found = {
  status: "found",
  evidence: "push",
  pull_request: {
    url: MAC_PR,
    number: 7,
    title: "Found on this Mac",
    state: "OPEN",
    is_draft: false,
    base_repository: "acme/app",
    head_repository: "acme/app",
    head_ref: "feature",
    head_sha: HEAD,
  },
  matches_head: true,
};

/**
 * A shell signed in as `mac-octo` that serves the session's context and answers
 * branch discovery with `branch`; `operations` and `contextServed` model older shells.
 */
function installShell({
  branch = found as unknown,
  operations = [...GITHUB_MAC_READ_OPERATIONS, "branch_pull_requests"],
  contextServed = true,
  answerContext = () => context(),
}: {
  branch?: unknown;
  operations?: readonly string[];
  contextServed?: boolean;
  answerContext?: () => unknown;
} = {}) {
  const codeHostQuery = vi.fn(async (request: { operation: string; host: string }) => {
    if (request.operation === "account_list") {
      return { ok: true, data: [{ login: "mac-octo", active: true, state: "success" }] };
    }
    if (request.operation === "branch_pull_requests") {
      return typeof branch === "function"
        ? (branch as () => unknown)()
        : {
            ok: true,
            data: branch,
            identity: { host: request.host, login: "mac-octo", generation: 1 },
          };
    }
    return { ok: false, error: { code: "unsupported", message: "Not in this test." } };
  });
  const workspaceRequest = vi.fn(async () => ({
    ok: true,
    status: 200,
    body: answerContext(),
    route: "workspace-helper",
  }));
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery,
    workspaceRequest,
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [{ id: "github", operations, accounts: true }],
      workspaceFastTrack: contextServed,
      workspaceFastTrackCapabilities: contextServed ? ["repository_context"] : [],
    }),
  };
  return { codeHostQuery, workspaceRequest };
}

/** The session's machine: its info lists the PR it inferred from the branch. */
function stubHost() {
  const fetchMock = vi.fn(async () => {
    const info = {
      object: "session.github.info",
      available: true,
      provider: "github",
      tracking_available: true,
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
      auth: {
        authenticated: true,
        hint: null,
        cli: null,
        accounts: [],
        selected_account: "host-octo",
      },
      capabilities: {
        account_switching: false,
        base_remote_selection: false,
        line_counts: true,
        linked_pr_diff: true,
      },
      pr: null,
    };
    return new Response(JSON.stringify(info));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const seen: { info?: PullRequestInfo } = {};

function Probe({ findBranch = true }: { findBranch?: boolean }) {
  seen.info = usePullRequestInfo("conv_1", { findBranch }).data;
  return null;
}

function renderProbe(props: { findBranch?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <Probe {...props} />
    </QueryClientProvider>,
  );
  return { client, view };
}

const urls = () => seen.info?.prs?.map((pr) => pr.url);
const branchCalls = (query: ReturnType<typeof installShell>["codeHostQuery"]) =>
  query.mock.calls.filter(([request]) => request.operation === "branch_pull_requests");

beforeEach(() => {
  seen.info = undefined;
  githubMacReadsPreference.write("on");
});

afterEach(() => {
  cleanup();
  githubMacReadsPreference.write("automatic");
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  vi.unstubAllGlobals();
  resetDesktopReadPathForTests();
  resetCodeHostContextReads();
  resetWorkspaceFastTrackForTests();
  resetHostTransportRecovery();
  health.online = true;
});

describe("the session branch's PR found on this Mac", () => {
  it("replaces the PR the session's machine inferred, and is selected", async () => {
    installShell();
    stubHost();
    renderProbe();
    await waitFor(() => expect(urls()).toEqual([MAC_PR]));
    expect(seen.info?.selected_pr_url).toBe(MAC_PR);
    expect(seen.info?.prs?.[0]).toMatchObject({ relationship: "inferred", provider: "github" });
  });

  it("shows while the session's machine can't be reached", async () => {
    installShell();
    const fetchMock = stubHost();
    health.online = false;
    renderProbe();
    await waitFor(() => expect(urls()).toEqual([MAC_PR]));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never falls back to the host's PR when this Mac is signed out", async () => {
    // The session's machine is signed in as someone else and would pick its own PR.
    const shell = installShell({
      branch: () => ({
        ok: false,
        error: {
          code: "signed_out",
          message: "No GitHub account for github.com is signed in on this Mac.",
        },
      }),
    });
    stubHost();
    renderProbe();
    await waitFor(() => expect(seen.info?.branch_discovery?.status).toBe("failed"));
    expect(urls()).toEqual([]);
    expect(seen.info?.selected_pr_url).toBeUndefined();
    expect(branchCalls(shell.codeHostQuery)).toHaveLength(1);
    // Only the user's choice moves it to the session's machine.
    act(() => findBranchOnSessionMachine("conv_1"));
    await waitFor(() => expect(urls()).toEqual([HOST_INFERRED]));
  });

  it("asks which PR when the evidence names several, in a notice", async () => {
    installShell({
      branch: {
        status: "ambiguous",
        candidates: [found.pull_request, { ...found.pull_request, url: `${MAC_PR}0`, number: 70 }],
        incomplete: false,
      },
    });
    stubHost();
    renderProbe();
    await waitFor(() => expect(seen.info?.branch_discovery?.status).toBe("ambiguous"));
    expect(urls()).toEqual([]);
  });
});

describe("while the session's machine is slow to answer", () => {
  it.each([
    ["the PR found", found, (info?: PullRequestInfo) => info?.prs?.map((pr) => pr.url), [MAC_PR]],
    [
      "the question which PR it is",
      {
        status: "ambiguous",
        candidates: [found.pull_request, { ...found.pull_request, url: `${MAC_PR}0`, number: 70 }],
        incomplete: false,
      },
      (info?: PullRequestInfo) => info?.branch_discovery?.status,
      "ambiguous",
    ],
  ] as const)("shows %s without waiting for it", async (_name, branch, read, expected) => {
    installShell({ branch });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    renderProbe();
    await waitFor(() => expect(read(seen.info)).toEqual(expected));
  });
});

describe("the session's machine still finds the branch's PR", () => {
  it.each([
    ["the setting is off", () => githubMacReadsPreference.write("off"), {}],
    ["the shell can't find branch PRs", () => {}, { operations: [...GITHUB_MAC_READ_OPERATIONS] }],
    ["the shell can't read the session's context", () => {}, { contextServed: false }],
    [
      "the context isn't available",
      () => {},
      { answerContext: () => ({ status: "unsupported_config" }) },
    ],
  ] as const)("when %s", async (_name, arrange, shellOptions) => {
    arrange();
    const shell = installShell(shellOptions);
    stubHost();
    renderProbe();
    await waitFor(() => expect(urls()).toEqual([HOST_INFERRED]));
    expect(seen.info?.branch_discovery).toBeUndefined();
    expect(branchCalls(shell.codeHostQuery)).toHaveLength(0);
  });

  it("for callers that don't ask, such as sidebar rows", async () => {
    const shell = installShell();
    stubHost();
    renderProbe({ findBranch: false });
    await waitFor(() => expect(urls()).toEqual([HOST_INFERRED]));
    expect(branchCalls(shell.codeHostQuery)).toHaveLength(0);
  });
});

describe("an answer for an older context", () => {
  it("is dropped once the branch moves", async () => {
    let key = "key-a";
    let answer: unknown = found;
    const shell = installShell({
      answerContext: () => context({ context_key: key }),
      branch: () => ({
        ok: true,
        data: answer,
        identity: { host: "github.com", login: "mac-octo", generation: 1 },
      }),
    });
    stubHost();
    const { client } = renderProbe();
    await waitFor(() => expect(urls()).toEqual([MAC_PR]));
    // A branch switch: the new context's discovery finds nothing.
    key = "key-b";
    answer = { status: "none" };
    await act(() => client.refetchQueries({ queryKey: ["code-host-context", "conv_1"] }));
    await waitFor(() => expect(urls()).toEqual([]));
    expect(branchCalls(shell.codeHostQuery).length).toBeGreaterThanOrEqual(2);
  });
});
