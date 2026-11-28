// A list row's Review…, Request Review… and Edit Labels…: each is offered where the control it
// opens would be, the Review menu's offer and the Reviewers and Labels sections' edit rules,
// and selects the PR, asking the view the row opens it in to open that control. Each needs the
// session's host to serve its picker's read and its change.

import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as OpenLinkInApp from "@/lib/openLinkInApp";
import type * as PageRouteModule from "@/pullRequestPage/pullRequestPageRoute";
import type { WorkspaceClient } from "@/lib/workspaceApi";
import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { resetControlRequestsForTests, waitingControlFor } from "@/pullRequestPage/controlRequests";
import { PullRequestRowDestinationContext } from "@/pullRequestPage/pullRequestDestination";
import { draftOwner } from "@/lib/workspaceDraftOwner";
import {
  pendingSubmitKey,
  readReviewSubmit,
  resetReviewSubmitForTests,
  reviewSubmitKey,
  updateReviewSubmit,
} from "@/pullRequestReviews/reviewSubmit";

import { usePullRequestListEntries } from "./usePullRequestListEntries";

const shell = vi.hoisted(() => ({ canEdit: true }));
const PICKER_METHODS = ["listLabels", "updateLabels", "listReviewerCandidates", "updateReviewers"];
const host = vi.hoisted(() => ({
  served: new Set<string>(),
  write: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<typeof PageRouteModule>()),
  usePullRequestMethodServed: (_sessionId: string, method: string) => host.served.has(method),
  hostChangeRoute: (conversationId: string, method: string) =>
    host.served.has(method) ? { conversationId, key: "route-1" } : null,
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  writePullRequestRpc: host.write,
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: host.toastError, dismiss: vi.fn() }),
}));
vi.mock("@/hooks/usePermissions", () => ({
  useCanEdit: () => shell.canEdit,
  useCanChangePullRequests: () => shell.canEdit,
}));
const opened = vi.hoisted(() => [] as string[]);
vi.mock("@/lib/openLinkInApp", async (importOriginal) => ({
  ...(await importOriginal<typeof OpenLinkInApp>()),
  openLink: (_id: string, url: string) => opened.push(url),
}));

const GITHUB_PR = "https://github.com/acme/app/pull/7";
let PR = GITHUB_PR;

/** Where the row's PR lives, and whether the row offers lifecycle changes as the page's do. */
interface RowTarget {
  url: string;
  provider?: string;
  lifecycleOnRows?: boolean;
  approveOnRows?: boolean;
  /** The host's account for the PR, and who the PR asks for a review. */
  hostLogin?: string;
  requested?: string[];
  /** A sub-agent that links the PR: the row reads through that session, not this list. */
  linkedBy?: { id: string; name: string };
}
const SCOPE = "pull-requests-page";

function rowFor(
  state: "OPEN" | "CLOSED" | "MERGED",
  capabilities = { label_editing: true, reviewer_editing: true },
  target: RowTarget = { url: GITHUB_PR },
) {
  PR = target.url;
  const onSelect = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  const info = {
    object: "session.github.info",
    ...(target.provider ? { provider: target.provider } : {}),
    available: true,
    selected_pr_url: PR,
    capabilities,
    auth: { selected_account: target.hostLogin ?? null, accounts: null },
    pr: {
      reviewers: (target.requested ?? []).map((login) => ({
        kind: "user",
        login,
        state: "PENDING",
      })),
      number: 7,
      title: "Build",
      state,
      url: PR,
      is_draft: false,
      author: "carol",
      base_ref: "main",
      head_ref: "feature",
      head_sha: "a".repeat(40),
      checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
    },
  };
  client.setQueryData(["pr-page-github-info", SCOPE, PR], info);
  // A sub-agent's row reads the PR through the sub-agent's session.
  if (target.linkedBy) client.setQueryData(["pr-page-github-info", target.linkedBy.id, PR], info);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <SidebarDataProvider>
        <PullRequestRowDestinationContext.Provider value="page-detail">
          {children}
        </PullRequestRowDestinationContext.Provider>
      </SidebarDataProvider>
    </QueryClientProvider>
  );
  const association = {
    url: PR,
    ...(target.provider ? { provider: target.provider } : {}),
    host: new URL(target.url).hostname,
    repository: "acme/app",
    number: 7,
    relationship: "created",
  } as unknown as PullRequestAssociation;
  const sessionSources = target.linkedBy
    ? new Map([[PR, { session: target.linkedBy, alsoIn: [] }]])
    : undefined;
  const { result } = renderHook(
    () =>
      usePullRequestListEntries(
        {
          conversationId: SCOPE,
          active: false,
          associations: [association],
          sessionSources,
          hostOffline: false,
          sandboxAsleep: false,
          onSelect,
          unlinkPending: false,
          managesSessionLinks: false,
        },
        { lifecycleOnRows: target.lifecycleOnRows, approveOnRows: target.approveOnRows },
      ),
    { wrapper },
  );
  const actions = result.current.actionsFor(result.current.entries[0]!);
  const action = (id: string) => actions.find((candidate) => candidate.id === id);
  return { onSelect, actions, action, commands: result.current.rowCommands };
}

beforeEach(() => {
  host.served = new Set(PICKER_METHODS);
  host.write.mockReset();
  host.toastError.mockReset();
  PR = GITHUB_PR;
  shell.canEdit = true;
  opened.length = 0;
});
afterEach(() => {
  resetControlRequestsForTests();
  resetReviewSubmitForTests();
});

describe("a row's Review…", () => {
  it("is Review on GitHub, opening GitHub and sending nothing", () => {
    const { onSelect, action, commands } = rowFor("OPEN");
    expect(action("review")).toBeUndefined();
    expect(action("review-on-provider")?.label).toBe("Review on GitHub");
    expect(commands.find((command) => command.id === `review-on-provider:${PR}`)?.label).toBe(
      "Review #7 Build on GitHub",
    );
    action("review-on-provider")!.run();
    expect(opened).toEqual([PR]);
    expect(onSelect).not.toHaveBeenCalled();
    expect(waitingControlFor("page-detail", PR)).toBeNull();
  });

  it("isn't offered on a closed or merged PR", () => {
    for (const state of ["CLOSED", "MERGED"] as const) {
      const { action } = rowFor(state);
      expect(action("review")).toBeUndefined();
      expect(action("review-on-provider")).toBeUndefined();
    }
  });
});

describe("a row's Request Review… and Edit Labels…", () => {
  it("open the Summary's pickers in the view the row opens, before the row's copies", () => {
    const { onSelect, actions, action, commands } = rowFor("OPEN");
    const ids = actions.map((candidate) => candidate.id);
    expect(ids.indexOf("edit-labels")).toBeLessThan(ids.indexOf("copy-link"));
    expect(commands.find((command) => command.id === `request-review:${PR}`)?.label).toBe(
      "Request Review on #7 Build…",
    );
    expect(commands.find((command) => command.id === `edit-labels:${PR}`)?.label).toBe(
      "Edit Labels on #7 Build…",
    );
    action("request-review")!.run();
    expect(onSelect).toHaveBeenCalledWith(PR, "here");
    expect(waitingControlFor("page-detail", PR)).toBe("reviewers");
    action("edit-labels")!.run();
    expect(waitingControlFor("page-detail", PR)).toBe("labels");
  });

  it("follow their sections on closed and merged PRs, where labels stay editable", () => {
    for (const state of ["CLOSED", "MERGED"] as const) {
      const { action } = rowFor(state);
      expect(action("edit-labels")).toBeDefined();
      expect(action("request-review")).toBeDefined();
    }
  });

  it("aren't offered where their sections can't edit", () => {
    const { action } = rowFor("OPEN", { label_editing: false, reviewer_editing: true });
    expect(action("edit-labels")).toBeUndefined();
    expect(action("request-review")).toBeDefined();
    shell.canEdit = false;
    const readOnly = rowFor("OPEN");
    expect(readOnly.action("edit-labels")).toBeUndefined();
    expect(readOnly.action("request-review")).toBeUndefined();
    // A sub-agent's row reads through its session, which this list can't change the PR through.
    shell.canEdit = true;
    const subAgent = rowFor("OPEN", undefined, {
      url: GITHUB_PR,
      linkedBy: { id: "conv_child", name: "auth-fix" },
    });
    expect(subAgent.action("edit-labels")).toBeUndefined();
    expect(subAgent.action("request-review")).toBeUndefined();
    expect(subAgent.commands.find((command) => command.id === `edit-labels:${PR}`)).toBeUndefined();
    expect(
      subAgent.commands.find((command) => command.id === `request-review:${PR}`),
    ).toBeUndefined();
  });

  it.each([
    ["listLabels", "edit-labels"],
    ["updateLabels", "edit-labels"],
    ["listReviewerCandidates", "request-review"],
    ["updateReviewers", "request-review"],
  ])("aren't offered where the host doesn't serve %s", (method, hidden) => {
    host.served.delete(method);
    const { action, commands } = rowFor("OPEN");
    expect(action(hidden)).toBeUndefined();
    expect(commands.find((command) => command.id === `${hidden}:${PR}`)).toBeUndefined();
    const other = hidden === "edit-labels" ? "request-review" : "edit-labels";
    expect(action(other)).toBeDefined();
  });
});

// Convert to draft, Ready for review and auto-merge, handed to GitHub where this Mac can't.
const LIFECYCLE_HANDOFF = /(draft|ready for review|auto-merge).* on GitHub$/i;

describe("a row's lifecycle handoffs", () => {
  const handoffs = (target: RowTarget) => {
    const { actions, commands } = rowFor("OPEN", undefined, { ...target, lifecycleOnRows: true });
    return {
      menu: actions.filter((action) => LIFECYCLE_HANDOFF.test(action.label)).map((a) => a.label),
      palette: commands
        .filter((command) => LIFECYCLE_HANDOFF.test(command.label))
        .map((c) => c.label),
    };
  };

  it("hands a GitHub PR's changes this Mac can't make to GitHub, in the menu and ⌘K", () => {
    expect(handoffs({ url: GITHUB_PR })).toEqual({
      menu: ["Convert to draft on GitHub", "Enable auto-merge on GitHub"],
      palette: ["Convert #7 Build to Draft on GitHub", "Enable Auto-merge for #7 Build on GitHub"],
    });
  });

  it.each([
    ["Azure DevOps", "azure_devops", "https://dev.azure.com/contoso/web/_git/app/pullrequest/7"],
    ["GitLab", "gitlab", "https://git.example.test/team/sub/project/-/merge_requests/7"],
  ])("offers no GitHub handoff for a PR on %s", (_name, provider, url) => {
    expect(handoffs({ url, provider })).toEqual({ menu: [], palette: [] });
  });
});

// Approve on a row of the new page's list, through the host's SubmitReviewVerdict.
describe("a row's Approve", () => {
  const ASKED = { url: GITHUB_PR, approveOnRows: true, hostLogin: "dana", requested: ["dana"] };

  /** The method and request of each host write sent so far. */
  async function sent(): Promise<[string, unknown][]> {
    const requests: [string, unknown][] = [];
    const workspace = new Proxy(
      {},
      {
        get: (_target, method: string) => async (request: unknown) => {
          requests.push([method, request]);
          return { payload: {} };
        },
      },
    ) as unknown as WorkspaceClient;
    await Promise.all(host.write.mock.calls.map(([, rpc]) => rpc.call(workspace)));
    return requests;
  }

  it("approves at once through the host where it serves verdicts and the PR asks its account", async () => {
    host.served = new Set(["submitReview", "submitReviewVerdict"]);
    host.write.mockResolvedValue({ object: "session.github.pr_review" });
    const { action, actions } = rowFor("OPEN", undefined, ASKED);
    const ids = actions.map((candidate) => candidate.id);
    expect(ids.indexOf("approve")).toBeLessThan(ids.indexOf("copy-link"));
    act(() => action("approve")!.run());
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));
    await expect(sent()).resolves.toEqual([
      ["submitReviewVerdict", { prUrl: PR, event: "APPROVE", body: "", commitOid: "a".repeat(40) }],
    ]);
  });

  it("says why where the host refuses it, offering the Review menu", async () => {
    const { PullRequestApiError } = await import("@/lib/pullRequestRpc");
    host.served = new Set(["submitReview", "submitReviewVerdict"]);
    host.write.mockRejectedValue(new PullRequestApiError("Reviews are locked.", 403));
    const { action } = rowFor("OPEN", undefined, ASKED);
    act(() => action("approve")!.run());
    await waitFor(() => expect(host.toastError).toHaveBeenCalledTimes(1));
    const [message, options] = host.toastError.mock.calls[0]!;
    expect(message).toBe("Couldn’t approve #7 Build: Reviews are locked.");
    expect(options.action.label).toBe("Open Review");
  });

  it("sends nothing after an unconfirmed approval, from the row or ⌘K, and opens the Review menu", async () => {
    const { PullRequestMaybeSentError } = await import("@/lib/workspaceApi");
    host.served = new Set(["submitReview", "submitReviewVerdict"]);
    host.write.mockRejectedValue(new PullRequestMaybeSentError("lost"));
    const { action, onSelect, commands } = rowFor("OPEN", undefined, ASKED);
    const key = reviewSubmitKey(draftOwner(), PR, "dana");
    act(() => action("approve")!.run());
    await waitFor(() => expect(readReviewSubmit(key).status.kind).toBe("uncertain"));
    const held = readReviewSubmit(key).status;
    act(() => action("approve")!.run());
    act(() => commands.find((command) => command.id === `approve:${PR}`)!.run());
    expect(host.write).toHaveBeenCalledTimes(1);
    expect(readReviewSubmit(key).status).toBe(held);
    expect(onSelect).toHaveBeenCalledWith(PR, "here");
    expect(waitingControlFor("page-detail", PR)).toBe("review");
  });

  it("sends nothing after a pending review's Request Changes lost its answer, and opens the Review menu", () => {
    host.served = new Set(["submitReview", "submitReviewVerdict", "getPendingReview"]);
    // Finish keeps a pending review's submit under the session host's identity.
    const key = pendingSubmitKey(
      draftOwner(),
      GITHUB_PR,
      { login: `host:${SCOPE}@route-1` },
      "PRR_1",
    );
    updateReviewSubmit(key, (stored) => ({
      ...stored,
      event: "REQUEST_CHANGES",
      body: "Fix it",
      status: {
        kind: "uncertain",
        sent: {
          event: "REQUEST_CHANGES",
          body: "Fix it",
          head: "a".repeat(40),
          knownReviewIds: [],
          sentAt: 1,
        },
        message: "lost",
        checking: false,
        checked: "never",
      },
    }));
    const { action, onSelect, commands } = rowFor("OPEN", undefined, ASKED);
    act(() => action("approve")!.run());
    act(() => commands.find((command) => command.id === `approve:${PR}`)!.run());
    expect(host.write).not.toHaveBeenCalled();
    expect(readReviewSubmit(key).status.kind).toBe("uncertain");
    expect(onSelect).toHaveBeenCalledWith(PR, "here");
  });

  it("keeps a review being written in the Review menu, sending nothing over it", () => {
    host.served = new Set(["submitReview", "submitReviewVerdict"]);
    const key = reviewSubmitKey(draftOwner(), PR, "dana");
    updateReviewSubmit(key, (stored) => ({ ...stored, event: "REQUEST_CHANGES", body: "Fix it" }));
    const { action, onSelect } = rowFor("OPEN", undefined, ASKED);
    act(() => action("approve")!.run());
    expect(host.write).not.toHaveBeenCalled();
    expect(readReviewSubmit(key)).toMatchObject({ event: "REQUEST_CHANGES", body: "Fix it" });
    expect(onSelect).toHaveBeenCalledWith(PR, "here");
  });

  it("isn't offered where the host doesn't serve verdicts", () => {
    host.served = new Set(["submitReview"]);
    expect(rowFor("OPEN", undefined, ASKED).action("approve")).toBeUndefined();
  });

  it("isn't offered where the PR doesn't ask the host's account, or the account wrote it", () => {
    host.served = new Set(["submitReview", "submitReviewVerdict"]);
    expect(
      rowFor("OPEN", undefined, { ...ASKED, requested: ["erin"] }).action("approve"),
    ).toBeUndefined();
    expect(
      rowFor("OPEN", undefined, { ...ASKED, hostLogin: "carol", requested: ["carol"] }).action(
        "approve",
      ),
    ).toBeUndefined();
    expect(rowFor("CLOSED", undefined, ASKED).action("approve")).toBeUndefined();
  });
});
