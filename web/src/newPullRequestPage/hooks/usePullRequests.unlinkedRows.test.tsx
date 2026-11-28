// Why a list row the session doesn't link can fail with "not associated": each case reads a row
// as the list does, against a server that refuses a PR the reading session doesn't link.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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

import { resetHostTransportRecovery } from "@/lib/hostTransportRecovery";
import {
  type LinkingSessionOf,
  type PullRequestInfoRead,
  usePullRequestInfos,
} from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PARENT = "conv_parent";
const CHILD = "conv_child";
const LINKED = "https://github.com/acme/app/pull/1";
// The branch's PR the list found; the server stores none of the session's links for it.
const FOUND = "https://github.com/acme/app/pull/7";
// A sub-agent's PR, inferred from its own branch: only the child session links it.
const CHILD_PR = "https://github.com/acme/app/pull/9";
const NOT_ASSOCIATED = "This pull request is not associated with the session";

function overview(url: string) {
  return {
    repository: "acme/app",
    pr: {
      number: Number(url.split("/").pop()),
      title: "Found",
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

/** The server: each session's host reads only the PRs that session links. */
function stubServer(links: Record<string, readonly string[]>) {
  const asked: { session: string; prUrl: string | null }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    const session = url.pathname.split("/")[3] ?? "";
    const prUrl = url.searchParams.get("pr_url");
    asked.push({ session, prUrl });
    const linked = links[session] ?? [];
    if (prUrl && !linked.includes(prUrl)) {
      return new Response(JSON.stringify({ detail: NOT_ASSOCIATED }), { status: 400 });
    }
    const info = {
      object: "session.github.info",
      available: true,
      provider: "github",
      tracking_available: true,
      repo: { name_with_owner: "acme/app" },
      prs: linked.map((prLink) => ({
        url: prLink,
        host: "github.com",
        repository: "acme/app",
        number: Number(prLink.split("/").pop()),
        relationship: "attached",
      })),
      selected_pr_url: prUrl ?? linked[0],
      auth: { authenticated: true, hint: null, cli: null, accounts: [], selected_account: "host" },
      capabilities: {
        account_switching: false,
        base_remote_selection: false,
        line_counts: true,
        linked_pr_diff: true,
      },
      pr: prUrl ? { ...overview(prUrl).pr, title: "Read on the session's machine" } : null,
    };
    return new Response(JSON.stringify(info));
  });
  vi.stubGlobal("fetch", fetchMock);
  return asked;
}

const seen: { reads?: PullRequestInfoRead[] } = {};

function Rows({
  sessionId,
  urls,
  linkingSessionOf,
}: {
  sessionId: string;
  urls: string[];
  linkingSessionOf?: LinkingSessionOf;
}) {
  seen.reads = usePullRequestInfos(sessionId, urls, { poll: false, linkingSessionOf });
  return null;
}

function renderRows(sessionId: string, urls: string[], linkingSessionOf?: LinkingSessionOf) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Rows sessionId={sessionId} urls={urls} linkingSessionOf={linkingSessionOf} />
    </QueryClientProvider>,
  );
}

async function settledRead(): Promise<PullRequestInfoRead> {
  await waitFor(() => {
    const read = seen.reads?.[0];
    expect(read?.data?.pr ?? read?.error).toBeTruthy();
  });
  return seen.reads![0]!;
}

afterEach(() => {
  cleanup();
  seen.reads = undefined;
  vi.unstubAllGlobals();
  resetHostTransportRecovery();
});

describe("a row the viewed session doesn't link", () => {
  it("is refused by the session's machine, which reads only the PRs it links", async () => {
    // The list shows this one as Not Linked, with Link to This Session for the owner.
    const asked = stubServer({ [PARENT]: [LINKED] });
    renderRows(PARENT, [FOUND]);

    const read = await settledRead();

    expect(read.error?.message).toContain(NOT_ASSOCIATED);
    expect(asked).toContainEqual({ session: PARENT, prUrl: FOUND });
  });

  it("reads a sub-agent's PR through the session that links it", async () => {
    const asked = stubServer({ [PARENT]: [LINKED], [CHILD]: [CHILD_PR] });
    // As the list names it, from the sub-agent's stored links on the server.
    renderRows(PARENT, [CHILD_PR], (url) => (url === CHILD_PR ? CHILD : undefined));

    const read = await settledRead();

    expect(read.error?.message ?? "").not.toContain(NOT_ASSOCIATED);
    expect(read.data?.pr?.url).toBe(CHILD_PR);
    expect(asked).toContainEqual({ session: CHILD, prUrl: CHILD_PR });
    expect(asked).not.toContainEqual({ session: PARENT, prUrl: CHILD_PR });
  });
});
