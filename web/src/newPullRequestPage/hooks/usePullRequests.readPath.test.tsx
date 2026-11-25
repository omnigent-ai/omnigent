// Which path a PR's queries read, with the real read-path store: a PR sent to the
// session's machine reads there, and a fetch that runs late reads the path its key names.

import { beginFreshReads } from "@/lib/pullRequestFreshReads";
import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { useReducer } from "react";
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

import {
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  chooseReadSource,
  readOnSessionMachine,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { resetHostTransportRecovery } from "@/lib/hostTransportRecovery";
import { ReadSourceUnavailableError } from "@/lib/pullRequestReadRoute";
import { usePullRequestReviewThreads } from "./usePullRequestReviewThreads";
import {
  usePullRequestChangedFiles,
  usePullRequestDiff,
  usePullRequestInfo,
  usePullRequestMergeOptions,
  usePullRequestRepoLabels,
  usePullRequestReviewCandidates,
  type PullRequestInfo,
} from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const GITHUB_PR = "https://github.com/example-org/universe/pull/42";
const GHES_PR = "https://ghe.example.com/team/project/pull/7";
const MAC_PATCH = "diff --git a/mac b/mac\n";
const HOST_PATCH = "diff --git a/host b/host\n";

function overview(url: string, title: string) {
  return {
    repository: new URL(url).pathname.split("/").slice(1, 3).join("/"),
    pr: {
      number: Number(url.split("/").pop()),
      title,
      state: "OPEN",
      url,
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

/** A shell signed in to every host as `mac-octo`, reading GitHub as the Mac's account. */
function installShell() {
  const codeHostQuery = vi.fn(
    async (request: { operation: string; host: string; params: Record<string, unknown> }) => {
      if (request.operation === "account_list") {
        return { ok: true, data: [{ login: "mac-octo", active: true, state: "success" }] };
      }
      const url = request.host === "github.com" ? GITHUB_PR : GHES_PR;
      const data =
        request.operation === "pull_request_patch"
          ? { patch: MAC_PATCH, base_sha: "b".repeat(40), head_sha: "a".repeat(40) }
          : overview(url, "Read on this Mac");
      return { ok: true, data, identity: { host: request.host, login: "mac-octo", generation: 1 } };
    },
  );
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
    },
    getDesktopFeatures: async () => ({
      codeHosts: [{ id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS], accounts: true }],
    }),
  };
  return codeHostQuery;
}

/** The session's machine: its info for each PR, and its patch. */
function stubHost() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    const prUrl = url.searchParams.get("pr_url");
    if (url.pathname.endsWith("/resources/github/diff")) {
      return new Response(JSON.stringify({ object: "session.github.pr_diff", patch: HOST_PATCH }));
    }
    const info = {
      object: "session.github.info",
      available: true,
      provider: "github",
      tracking_available: true,
      repo: { name_with_owner: "example-org/universe" },
      prs: [],
      auth: { authenticated: true, hint: null, cli: null, accounts: [], selected_account: "host" },
      capabilities: {
        account_switching: false,
        base_remote_selection: false,
        line_counts: true,
        linked_pr_diff: true,
      },
      ...(prUrl
        ? { selected_pr_url: prUrl, pr: overview(prUrl, "Read by the session's machine").pr }
        : { pr: null }),
    };
    return new Response(JSON.stringify(info));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const seen: { info?: PullRequestInfo; patch?: string } = {};

function Probe({ url }: { url: string }) {
  seen.info = usePullRequestInfo("conv_1", { prUrl: url }).data;
  seen.patch = usePullRequestDiff("conv_1", true, url).data?.patch;
  return null;
}

function renderProbe(url: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Probe url={url} />
    </QueryClientProvider>,
  );
  return client;
}

beforeEach(() => {
  seen.info = undefined;
  seen.patch = undefined;
});

afterEach(() => {
  cleanup();
  onlineManager.setOnline(true);
  githubMacReadsPreference.write("automatic");
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  vi.unstubAllGlobals();
});

describe("a PR sent to the session's machine", () => {
  it.each([
    ["github.com", GITHUB_PR],
    ["a known Enterprise host", GHES_PR],
  ])("on %s reads there while this Mac is still signed in", async (_name, url) => {
    installShell();
    stubHost();
    githubMacReadsPreference.write("on");
    renderProbe(url);
    await waitFor(() => expect(seen.info?.read_on_mac?.login).toBe("mac-octo"));
    expect(seen.patch).toBe(MAC_PATCH);

    act(() => readOnSessionMachine(url));
    await waitFor(() => expect(seen.info?.pr?.title).toBe("Read by the session's machine"));
    expect(seen.info?.read_on_mac).toBeUndefined();
    await waitFor(() => expect(seen.patch).toBe(HOST_PATCH));
  });
});

describe("a PR chosen for this Mac while Settings leave GitHub on the host", () => {
  afterEach(() => resetDesktopReadPathForTests());

  it.each([
    ["github.com", GITHUB_PR],
    ["an Enterprise host this Mac is signed in to", GHES_PR],
  ])(
    "on %s settles and reads on this Mac, asking about the host at most once",
    async (_name, url) => {
      const query = installShell();
      stubHost();
      githubMacReadsPreference.write("off");
      act(() => chooseReadSource(url, "mac"));
      renderProbe(url);
      await waitFor(() => expect(seen.info?.read_on_mac?.login).toBe("mac-octo"));
      expect(seen.info?.pr?.title).toBe("Read on this Mac");
      await waitFor(() => expect(seen.patch).toBe(MAC_PATCH));
      const probes = query.mock.calls.filter(([request]) => request.operation === "account_list");
      expect(probes.length).toBeLessThanOrEqual(1);
    },
  );
});

describe("a PR chosen for this Mac that this Mac can't read", () => {
  /** A shell that reads GitSpoke only, as an older one might: github.com has no Mac path. */
  function installGitSpokeOnlyShell() {
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      codeHostQuery: vi.fn(async () => ({ ok: false, error: { code: "unsupported" } })),
      getDesktopFeatures: async () => ({
        codeHosts: [{ id: "gitspoke", operations: ["pull_request_overview"] }],
      }),
    };
  }

  const errors: Record<string, unknown> = {};
  function EveryRead({ url }: { url: string }) {
    errors.info = usePullRequestInfo("conv_1", { prUrl: url }).error;
    errors.changes = usePullRequestChangedFiles("conv_1", true, url).error;
    errors.diff = usePullRequestDiff("conv_1", true, url).error;
    errors.threads = usePullRequestReviewThreads("conv_1", url, null, "b:a", true).error;
    errors.labels = usePullRequestRepoLabels("conv_1", url, true).error;
    errors.merge = usePullRequestMergeOptions("conv_1", url, true).error;
    errors.reviewers = usePullRequestReviewCandidates("conv_1", url, true).error;
    return null;
  }

  afterEach(() => resetDesktopReadPathForTests());

  it("asks the session's machine nothing about the PR, on any read", async () => {
    installGitSpokeOnlyShell();
    const fetchMock = stubHost();
    act(() => chooseReadSource(GITHUB_PR, "mac"));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <EveryRead url={GITHUB_PR} />
      </QueryClientProvider>,
    );
    await waitFor(() => {
      for (const read of ["info", "changes", "diff", "labels", "merge", "reviewers"]) {
        expect(errors[read], read).toBeInstanceOf(ReadSourceUnavailableError);
      }
    });
    const askedForThePr = fetchMock.mock.calls.filter(([input]) =>
      decodeURIComponent(String(input)).includes(GITHUB_PR),
    );
    expect(askedForThePr).toEqual([]);
  });

  it("never shows the host's earlier answer once this Mac is chosen and can't read", async () => {
    installGitSpokeOnlyShell();
    const fetchMock = stubHost();
    const client = renderProbe(GITHUB_PR);
    await waitFor(() => expect(seen.info?.pr?.title).toBe("Read by the session's machine"));
    const askedBefore = fetchMock.mock.calls.length;

    act(() => chooseReadSource(GITHUB_PR, "mac"));
    const blocked = () =>
      client
        .getQueryCache()
        .findAll({ queryKey: ["github-info", "conv_1", GITHUB_PR] })
        .find((query) => query.state.error instanceof ReadSourceUnavailableError);
    await waitFor(() => expect(blocked()).toBeDefined());
    expect(seen.info).toBeUndefined();
    expect(seen.patch).toBeUndefined();
    expect(fetchMock.mock.calls.length).toBe(askedBefore);
    // The host's answer stays cached under its own key, ready if the user goes back.
    act(() => chooseReadSource(GITHUB_PR, "session"));
    await waitFor(() => expect(seen.info?.pr?.title).toBe("Read by the session's machine"));
  });
});

describe("a first read through a PR's subpage on an Enterprise host this Mac hasn't seen", () => {
  afterEach(() => resetDesktopReadPathForTests());

  it.each(["files", "commits"])(
    "settles and reads on this Mac from its /%s page, with Settings off and this Mac chosen",
    async (subpage) => {
      const query = installShell();
      stubHost();
      githubMacReadsPreference.write("off");
      const url = `${GHES_PR}/${subpage}`;
      act(() => chooseReadSource(url, "mac"));
      renderProbe(url);
      await waitFor(() => expect(seen.info?.read_on_mac?.login).toBe("mac-octo"));
      const probes = query.mock.calls.filter(([request]) => request.operation === "account_list");
      expect(probes.length).toBeLessThanOrEqual(1);
    },
  );
});

describe("a blocked PR while the session's machine is offline", () => {
  afterEach(() => {
    health.online = true;
    resetDesktopReadPathForTests();
  });

  it("settles at once with the blocked error, asking nobody", async () => {
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      codeHostQuery: vi.fn(async () => ({ ok: false, error: { code: "unsupported" } })),
      getDesktopFeatures: async () => ({
        codeHosts: [{ id: "gitspoke", operations: ["pull_request_overview"] }],
      }),
    };
    const fetchMock = stubHost();
    health.online = false;
    act(() => chooseReadSource(GITHUB_PR, "mac"));
    const client = renderProbe(GITHUB_PR);
    await waitFor(() =>
      expect(
        client
          .getQueryCache()
          .findAll({ queryKey: ["github-info", "conv_1", GITHUB_PR] })
          .some((query) => query.state.error instanceof ReadSourceUnavailableError),
      ).toBe(true),
    );
    const askedForThePr = fetchMock.mock.calls.filter(([input]) =>
      decodeURIComponent(String(input)).includes(GITHUB_PR),
    );
    expect(askedForThePr).toEqual([]);
  });
});

describe("a host read already under way when the PR becomes blocked", () => {
  afterEach(() => resetDesktopReadPathForTests());

  it("is refused at the host boundary on its retry, so the host isn't asked again", async () => {
    installShell();
    githubMacReadsPreference.write("on");
    // The session machine's context for this Mac's PR fails once, as a host that's starting.
    let contextAsks = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.searchParams.get("pr_url") === GITHUB_PR) {
        contextAsks += 1;
        return new Response(JSON.stringify({ error: "starting" }), { status: 502 });
      }
      return new Response(JSON.stringify({ object: "session.github.info", available: false }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderProbe(GITHUB_PR);
    await waitFor(() => expect(seen.info?.read_on_mac?.login).toBe("mac-octo"));
    await waitFor(() => expect(contextAsks).toBeGreaterThan(0));
    const asked = contextAsks;

    // This Mac stops reading GitHub, as after a downgrade, while the PR is chosen for it.
    act(() => {
      resetDesktopReadPathForTests();
      (window as unknown as Record<string, unknown>).omnigentDesktop = {
        kind: "electron",
        getDesktopFeatures: async () => ({
          codeHosts: [{ id: "gitspoke", operations: ["pull_request_overview"] }],
        }),
      };
      chooseReadSource(GITHUB_PR, "mac");
    });
    await new Promise((resolve) => {
      setTimeout(resolve, 1500);
    });
    expect(contextAsks).toBe(asked);
  });
});

describe("the session machine's controls for a PR this Mac reads", () => {
  it("never ask the session's machine while it can't be reached", async () => {
    installShell();
    const fetchMock = stubHost();
    githubMacReadsPreference.write("on");
    health.online = false;
    try {
      renderProbe(GITHUB_PR);
      await waitFor(() => expect(seen.info?.read_on_mac?.login).toBe("mac-octo"));
      await new Promise((resolve) => {
        setTimeout(resolve, 50);
      });
      const askedForThePr = fetchMock.mock.calls.filter(([input]) =>
        String(input).includes("pr_url="),
      );
      expect(askedForThePr).toEqual([]);
    } finally {
      health.online = true;
      resetHostTransportRecovery();
    }
  });
});

describe("a fetch that runs after the read path changed", () => {
  it.each([
    { from: "on", to: "off", reads: MAC_PATCH },
    { from: "off", to: "on", reads: HOST_PATCH },
  ] as const)("reads the path its key named ($from → $to)", async ({ from, to, reads }) => {
    installShell();
    stubHost();
    githubMacReadsPreference.write(from);
    // The shell has answered what it reads, as it has long before a panel opens.
    await desktopReadTarget(GITHUB_PR);
    // Paused offline, so the first key's reader hasn't run yet.
    onlineManager.setOnline(false);
    const client = renderProbe(GITHUB_PR);
    const diffs = () =>
      client.getQueryCache().findAll({ queryKey: ["github-pr-diff", "conv_1", GITHUB_PR] });
    await waitFor(() => expect(diffs()).toHaveLength(1));
    const [first] = diffs();
    act(() => githubMacReadsPreference.write(to));
    await waitFor(() => expect(diffs()).toHaveLength(2));
    // However late it runs, the first key's fetch reads the path that key names.
    const queryFn = first.options.queryFn as (context: unknown) => Promise<{ patch: string }>;
    onlineManager.setOnline(true);
    // As TanStack calls it: with its client, which places the read among the shell's.
    const answer = await queryFn({
      client,
      queryKey: first.queryKey,
      signal: new AbortController().signal,
    });
    expect(answer.patch).toBe(reads);
  });
});

describe("the info for a PR this Mac reads", () => {
  it("stays the same object across renders, so effects that watch it settle", async () => {
    installShell();
    const fetchMock = stubHost();
    // The session lists the PR too, as it does once linked.
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      const prUrl = url.searchParams.get("pr_url");
      return new Response(
        JSON.stringify({
          object: "session.github.info",
          available: true,
          provider: "github",
          tracking_available: true,
          repo: { name_with_owner: "example-org/universe" },
          prs: [
            {
              url: GITHUB_PR,
              host: "github.com",
              repository: "example-org/universe",
              number: 42,
            },
          ],
          auth: {
            authenticated: true,
            hint: null,
            cli: null,
            accounts: [],
            selected_account: "host",
          },
          ...(prUrl
            ? { selected_pr_url: prUrl, pr: overview(prUrl, "Read by the session's machine").pr }
            : { pr: null }),
        }),
      );
    });
    githubMacReadsPreference.write("on");
    const seenData: (PullRequestInfo | undefined)[] = [];
    let rerender: () => void = () => undefined;
    function Rerendering() {
      const [, force] = useReducer((count: number) => count + 1, 0);
      rerender = force;
      seenData.push(usePullRequestInfo("conv_1", { prUrl: GITHUB_PR }).data);
      return null;
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Rerendering />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(seenData.at(-1)?.read_on_mac?.login).toBe("mac-octo"));
    await waitFor(() => expect(seenData.at(-1)?.prs?.length).toBe(1));
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    const settled = seenData.at(-1);
    for (let pass = 0; pass < 3; pass++) act(() => rerender());
    expect(seenData.slice(-3).every((data) => data === settled)).toBe(true);
  });
});

describe("a patch read on this Mac", () => {
  const patchReads = (query: ReturnType<typeof installShell>) =>
    query.mock.calls
      .map(([request]) => request)
      .filter((request) => request.operation === "pull_request_patch")
      .map((request) => request.params);

  it("lets the shell answer what it kept, except while a Refresh reads the PR again", async () => {
    const query = installShell();
    stubHost();
    githubMacReadsPreference.write("on");
    const client = renderProbe(GITHUB_PR);
    await waitFor(() => expect(seen.patch).toBe(MAC_PATCH));
    expect(patchReads(query).at(-1)).toMatchObject({ patch_cache: 1, fresh: false });

    const endFreshReads = beginFreshReads(GITHUB_PR);
    await act(() => client.invalidateQueries({ queryKey: ["github-pr-diff"] }));
    await waitFor(() => expect(patchReads(query).at(-1)).toMatchObject({ fresh: true }));
    endFreshReads();

    const before = patchReads(query).length;
    await act(() => client.invalidateQueries({ queryKey: ["github-pr-diff"] }));
    await waitFor(() => expect(patchReads(query).length).toBeGreaterThan(before));
    expect(patchReads(query).at(-1)).toMatchObject({ patch_cache: 1, fresh: false });
  });
});
