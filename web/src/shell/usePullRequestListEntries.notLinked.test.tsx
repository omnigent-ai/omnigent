// A row the session's machine won't read because the session doesn't link its PR shows "Not
// Linked". Only the session's owner is offered Link to This Session, in the row's menu and ⌘K;
// a refusal from the server shows its message and offers no retry. A sub-agent's row reads
// through the session that links it, and offers no change this list's session would send.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as Permissions from "@/hooks/usePermissions";
import type * as PullRequestsModule from "@/newPullRequestPage/hooks/usePullRequests";
import type * as PageRouteModule from "@/pullRequestPage/pullRequestPageRoute";
import type { Method } from "@/lib/workspaceApi";
import type * as LinkAvailability from "./linkPullRequestAvailability";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import {
  PullRequestApiError,
  type PullRequestAssociation,
  type PullRequestInfoRead,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { PullRequestSessionSource } from "@/pullRequestList/subAgentPullRequests";
import { pageStatus } from "@/pullRequestList/pullRequestListPageModel";
import { PullRequestListPageRow } from "@/pullRequestList/PullRequestListPageRow";
import { sessionPullRequestsKey } from "@/newPullRequestPage/hooks/useSessionPullRequests";
import { TooltipProvider } from "@/components/ui/tooltip";
import { NOT_LINKED_DETAIL, NOT_LINKED_REFUSAL } from "@/pullRequestList/notLinkedRow";
import { AVAILABLE, isCommandAvailable } from "@/store/paletteCommands";
import { usePullRequestAttachStore } from "@/store/pullRequestAttach";

import type { PullRequestListEntry, RowAction } from "./PullRequestListRows";
import { type PullRequestListProps, usePullRequestListEntries } from "./usePullRequestListEntries";

vi.mock("@/hooks/usePermissions", async (importOriginal) => ({
  ...(await importOriginal<typeof Permissions>()),
  useCanEdit: () => true,
  useCanChangePullRequests: () => true,
}));
// The viewer may edit the session; ownership is what decides the link.
vi.mock("./linkPullRequestAvailability", async (importOriginal) => ({
  ...(await importOriginal<typeof LinkAvailability>()),
  useLinkPullRequestAvailability: () => AVAILABLE,
}));
// Each row's read as the case sets it; the real reads where a case sets none.
const reads: { of?: (url: string) => PullRequestInfoRead } = {};
vi.mock("@/newPullRequestPage/hooks/usePullRequests", async (importOriginal) => {
  const actual = await importOriginal<typeof PullRequestsModule>();
  return {
    ...actual,
    usePullRequestInfos: (...args: Parameters<typeof actual.usePullRequestInfos>) => {
      const real = actual.usePullRequestInfos(...args);
      return reads.of ? args[1].map((url) => reads.of!(url)) : real;
    },
  };
});

// Where a case says so, every session's host serves every change.
const hosts = vi.hoisted(() => ({ serveAll: false }));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => {
  const actual = await importOriginal<typeof PageRouteModule>();
  const route = { route: "server", key: "server:route", served: new Set<string>() };
  return {
    ...actual,
    usePullRequestPageRoute: (sessionId: string) =>
      hosts.serveAll ? route : actual.usePullRequestPageRoute(sessionId),
    usePullRequestMethodServed: (sessionId: string, method: Method) =>
      hosts.serveAll || actual.usePullRequestMethodServed(sessionId, method),
    hostChangeRoute: (sessionId: string, method: Method) =>
      hosts.serveAll
        ? { conversationId: sessionId, key: route.key }
        : actual.hostChangeRoute(sessionId, method),
  };
});

const SESSION = "conv_parent";
const PR = "https://github.com/acme/app/pull/7";
const CHILD = { id: "conv_child", name: "auth-fix" };
const OWNER_ONLY =
  "Only the session's owner can change its pull requests. Open the pull request on GitHub to make changes there.";

const association = {
  url: PR,
  host: "github.com",
  repository: "acme/app",
  number: 7,
  relationship: "inferred",
} as unknown as PullRequestAssociation;

function read(over: Partial<PullRequestInfoRead>): PullRequestInfoRead {
  return {
    data: undefined,
    error: null,
    isError: false,
    isFetching: false,
    isLoading: false,
    isEnabled: true,
    isStale: false,
    dataUpdatedAt: 0,
    refetch: async () => undefined,
    ...over,
  };
}

const refused = () =>
  read({ error: new PullRequestApiError(NOT_LINKED_REFUSAL, 400), isError: true });

/** A row that loaded: nothing needs linking for it to show. */
const loadedRow = () =>
  read({
    data: {
      object: "session.github.info",
      available: true,
      provider: "github",
      tracking_available: true,
      prs: [],
      selected_pr_url: PR,
      auth: { authenticated: true, hint: null, cli: null, accounts: [], selected_account: null },
      capabilities: {},
      pr: {
        number: 7,
        title: "Loaded",
        state: "OPEN",
        url: PR,
        is_draft: false,
        author: "author",
        base_ref: "main",
        head_ref: "feature",
        head_sha: "a".repeat(40),
        checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
        comments: [],
        reviewers: [],
        labels: [],
      },
    } as never,
    dataUpdatedAt: 1,
  });

function renderList(over: Partial<PullRequestListProps> = {}, client?: QueryClient) {
  const queryClient =
    client ?? new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <SidebarDataProvider>{children}</SidebarDataProvider>
    </QueryClientProvider>
  );
  const { result } = renderHook(
    () =>
      usePullRequestListEntries({
        conversationId: SESSION,
        active: false,
        associations: [association],
        hostOffline: false,
        sandboxAsleep: false,
        onSelect: vi.fn(),
        onUnlink: vi.fn(),
        unlinkPending: false,
        ...over,
      }),
    { wrapper },
  );
  const view = () => {
    const entry = result.current.entries[0]!;
    return {
      actions: result.current.actionsFor(entry),
      entry,
      status: pageStatus(entry, { sandboxAsleep: false, mergeRequested: false }),
      link: result.current.actionsFor(entry).find((action) => action.id === "link"),
      paletteLink: result.current.rowCommands.find((command) => command.id === `link:${PR}`),
    };
  };
  return { view, queryClient };
}

afterEach(() => {
  reads.of = undefined;
  hosts.serveAll = false;
  usePullRequestAttachStore.setState({ attempts: {}, drafts: {}, lastAttached: null });
  vi.unstubAllGlobals();
});

describe("a row the session's machine won't read because the session doesn't link it", () => {
  it("says Not Linked, and offers its owner Link to This Session in the row, its menu and ⌘K", () => {
    reads.of = refused;
    const { view } = renderList({ ownsSessionTree: true });
    const { status, link, paletteLink } = view();

    expect(status).toMatchObject({ label: "Not Linked", meaning: "not-linked", tone: "muted" });
    expect(status.detail).toBe(NOT_LINKED_DETAIL);
    expect(link).toMatchObject({ label: "Link to this session" });
    expect(isCommandAvailable(link!.availability)).toBe(true);
    expect(paletteLink).toMatchObject({ label: "Link #7 to this session" });
    expect(isCommandAvailable(paletteLink!.availability)).toBe(true);
  });

  it.each([
    [false, "a non-owner"],
    [undefined, "a viewer whose ownership is still being read"],
  ])("says Not Linked with no action where ownership is %s (%s)", (owns, _viewer) => {
    reads.of = refused;
    const { view } = renderList({ ownsSessionTree: owns });
    const { status, link, paletteLink } = view();

    expect(status.label).toBe("Not Linked");
    expect(link).toBeUndefined();
    expect(paletteLink).toBeUndefined();
  });

  it("offers a non-owner no action for a row that loaded", () => {
    reads.of = loadedRow;
    const { view } = renderList({ ownsSessionTree: false });
    const { status, link, paletteLink } = view();

    expect(status.label).not.toBe("Not Linked");
    expect(link).toBeUndefined();
    expect(paletteLink).toBeUndefined();
  });

  it("offers nothing to link on a sub-agent's row, which belongs to that sub-agent", () => {
    reads.of = refused;
    const source: PullRequestSessionSource = { session: CHILD, alsoIn: [] };
    const { view } = renderList({
      ownsSessionTree: true,
      sessionSources: new Map([[PR, source]]),
    });

    expect(view().link).toBeUndefined();
  });

  it("shows the server's refusal of a non-owner in place, keeps Not Linked, and offers no retry", async () => {
    reads.of = refused;
    const asked = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(JSON.stringify({ detail: OWNER_ONLY }), { status: 403 }),
    );
    vi.stubGlobal("fetch", asked);
    // The client still thinks the viewer owns the session; the server knows better.
    const { view } = renderList({ ownsSessionTree: true });

    act(() => view().link!.run());

    await waitFor(() => expect(view().entry.linkFailure).toBe(OWNER_ONLY));
    const posted = asked.mock.calls
      .filter(([, init]) => init?.method === "POST")
      .map(([input]) => new URL(String(input), "http://localhost").pathname);
    expect(posted).toContain(`/v1/sessions/${SESSION}/resources/github/prs`);
    const { status, link, paletteLink } = view();
    expect(status).toMatchObject({ label: "Not Linked", detail: OWNER_ONLY });
    expect(link).toBeUndefined();
    expect(paletteLink).toBeUndefined();
  });
});

it("reads a sub-agent's row through the session that links it", () => {
  const source: PullRequestSessionSource = { session: CHILD, alsoIn: [] };
  const { queryClient } = renderList({ sessionSources: new Map([[PR, source]]) });

  const rowKeys = queryClient
    .getQueryCache()
    .findAll({ queryKey: ["pr-page-github-info"] })
    .map((query) => query.queryKey)
    // A key part equal to the PR's URL, compared whole.
    .filter((key) => key.some((part) => part === PR));
  expect(rowKeys).toContainEqual(expect.arrayContaining(["pr-page-github-info", CHILD.id, PR]));
  expect(rowKeys.some((key) => key[1] === SESSION)).toBe(false);
});

it("offers a sub-agent's row no change, which this list's session would send and be refused", () => {
  reads.of = loadedRow;
  hosts.serveAll = true;
  const changes = (sessionSources?: Map<string, PullRequestSessionSource>) => {
    const { result } = renderHook(
      () =>
        usePullRequestListEntries(
          {
            conversationId: SESSION,
            active: false,
            associations: [association],
            hostOffline: false,
            sandboxAsleep: false,
            onSelect: vi.fn(),
            onUnlink: vi.fn(),
            unlinkPending: false,
            sessionSources,
          },
          { lifecycleOnRows: true, approveOnRows: true },
        ),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <QueryClientProvider
            client={new QueryClient({ defaultOptions: { queries: { enabled: false } } })}
          >
            <SidebarDataProvider>{children}</SidebarDataProvider>
          </QueryClientProvider>
        ),
      },
    );
    const ids = result.current.actionsFor(result.current.entries[0]!).map((action) => action.id);
    return ids.filter((id) => id === "close" || id.startsWith("lifecycle:"));
  };

  expect(changes()).toEqual(expect.arrayContaining(["lifecycle:convert_to_draft", "close"]));
  const source: PullRequestSessionSource = { session: CHILD, alsoIn: [] };
  expect(changes(new Map([[PR, source]]))).toEqual([]);
});

/** The row as the list page draws it, with its actions. */
function renderRow({ entry, actions }: { entry: PullRequestListEntry; actions: RowAction[] }) {
  render(
    <TooltipProvider>
      <div role="grid">
        <PullRequestListPageRow
          entry={entry}
          actions={actions}
          now={0}
          repository={undefined}
          personHues={new Map()}
          sandboxAsleep={false}
          mergeRequested={false}
          managesSessionLinks
          selectable={false}
          isSelected={false}
          isActive={false}
          isShown={false}
          domId="row"
          onActivate={vi.fn()}
          onOpen={vi.fn()}
          onToggle={vi.fn()}
        />
      </div>
    </TooltipProvider>,
  );
}

describe("a branch-found row that loads", () => {
  it("keeps From branch and offers its owner Link to This Session in its menu and ⌘K, not in the row", () => {
    reads.of = loadedRow;
    const { view } = renderList({ ownsSessionTree: true });
    const current = view();

    expect(current.status.label).not.toBe("Not Linked");
    expect(current.entry.row.inferred).toBe(true);
    expect(isCommandAvailable(current.link!.availability)).toBe(true);
    expect(isCommandAvailable(current.paletteLink!.availability)).toBe(true);
    renderRow(current);
    expect(screen.getByText("From branch")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Link #7/ })).toBeNull();
  });

  it("offers nothing to link once the session links it", () => {
    reads.of = loadedRow;
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, enabled: false } },
    });
    client.setQueryData(sessionPullRequestsKey(SESSION), {
      prs: [{ ...association, relationship: "attached" }],
      synced: true,
    });
    const { view } = renderList({ ownsSessionTree: true }, client);

    expect(view().link).toBeUndefined();
    expect(view().paletteLink).toBeUndefined();
  });
});

it("shows Link to This Session in a Not Linked row for its owner, and not for anyone else", () => {
  reads.of = refused;
  const owner = renderList({ ownsSessionTree: true }).view();
  renderRow(owner);
  expect(screen.getByRole("button", { name: "Link #7 to this session" })).toBeEnabled();
  cleanup();
  renderRow(renderList({ ownsSessionTree: false }).view());
  expect(screen.queryByRole("button", { name: "Link #7 to this session" })).toBeNull();
});
