// A list row's Review…, Request Review… and Edit Labels…: each is offered where the control it
// opens would be, the Review menu's offer and the Reviewers and Labels sections' edit rules,
// and selects the PR, asking the view the row opens it in to open that control.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as DesktopPullRequestWrites from "@/lib/desktopPullRequestWrites";
import type * as OpenLinkInApp from "@/lib/openLinkInApp";
import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { readScope } from "@/lib/desktopReadPath";
import {
  resetControlRequestsForTests,
  takeControlRequest,
  waitingControlFor,
} from "@/pullRequestPage/controlRequests";
import { PullRequestRowDestinationContext } from "@/pullRequestPage/pullRequestDestination";

import { usePullRequestListEntries } from "./usePullRequestListEntries";

const shell = vi.hoisted(() => ({ reviews: false, canEdit: true, labelsInFlight: false }));
vi.mock("@/lib/desktopPullRequestWrites", async (importOriginal) => {
  const actual = await importOriginal<typeof DesktopPullRequestWrites>();
  const route = (url: string) => ({
    target: { provider: "github", host: "github.com", repository: "acme/app", number: 7, url },
    actor: { login: "octo", generation: 1 },
    reader: null,
  });
  return {
    ...actual,
    macWriteRoute: (url: string) => (shell.reviews ? route(url) : null),
    macReviewWriteOffers: () => shell.reviews,
    macWriteInFlightFor: (url: string, operation: string) =>
      operation === "pull_request_labels_update"
        ? shell.labelsInFlight
        : actual.macWriteInFlightFor(url, operation as never),
  };
});
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
  client.setQueryData(["github-info", SCOPE, PR, ...readScope(PR, null)], {
    object: "session.github.info",
    ...(target.provider ? { provider: target.provider } : {}),
    available: true,
    selected_pr_url: PR,
    capabilities,
    pr: {
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
  });
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
  const { result } = renderHook(
    () =>
      usePullRequestListEntries(
        {
          conversationId: SCOPE,
          active: false,
          associations: [association],
          hostOffline: false,
          sandboxAsleep: false,
          onSelect,
          unlinkPending: false,
          managesSessionLinks: false,
        },
        { lifecycleOnRows: target.lifecycleOnRows },
      ),
    { wrapper },
  );
  const actions = result.current.actionsFor(result.current.entries[0]!);
  const action = (id: string) => actions.find((candidate) => candidate.id === id);
  return { onSelect, actions, action, commands: result.current.rowCommands };
}

beforeEach(() => {
  PR = GITHUB_PR;
  shell.reviews = false;
  shell.canEdit = true;
  shell.labelsInFlight = false;
  opened.length = 0;
});
afterEach(() => resetControlRequestsForTests());

describe("a row's Review…", () => {
  it("opens the PR's Review menu where this Mac reviews it, in the view the row opens", () => {
    shell.reviews = true;
    const { onSelect, action, commands } = rowFor("OPEN");
    expect(action("review")).toMatchObject({ label: "Review…", movesFocus: true });
    expect(commands.find((command) => command.id === `review:${PR}`)?.label).toBe(
      "Review #7 Build…",
    );
    action("review")!.run();
    expect(onSelect).toHaveBeenCalledWith(PR, "here");
    expect(waitingControlFor("page-detail", PR)).toBe("review");
    expect(
      takeControlRequest(PR, "review", { destination: "page-detail", reader: null })?.detail,
    ).toBeNull();
  });

  it("is Review on GitHub where this Mac can't, opening GitHub and sending nothing", () => {
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

  it("hold Edit Labels…, with the picker's reason, while a label change is still going", () => {
    expect(rowFor("OPEN").action("edit-labels")?.availability).toEqual({ available: true });
    shell.labelsInFlight = true;
    const { action, commands } = rowFor("OPEN");
    const waiting = { available: false, reason: "Wait for the last label change to finish" };
    expect(action("edit-labels")?.availability).toEqual(waiting);
    expect(commands.find((command) => command.id === `edit-labels:${PR}`)?.availability).toEqual(
      waiting,
    );
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
