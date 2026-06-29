// Integration tests for the Sidebar's session list. The search box no
// longer carries a filter funnel (agent-type filter + "Show archived"
// toggle were removed). The sidebar fetches a single session list with
// archived sessions included, rendering the non-archived ones as grouped
// sections (Pinned / Projects / Chats / Shared with me). Archived sessions
// are no longer listed here — they live on the Settings page.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";

// Project mocks are declared via vi.hoisted so they exist before the hoisted
// vi.mock factory runs. projectsMock is mutated per-test to drive project
// sections; moveToProjectSpy captures kebab-menu "Change project" calls.
const {
  projectsMock,
  moveToProjectSpy,
  deleteProjectSpy,
  fetchProjectSessionIdsMock,
  conversationsRef,
  projectSessionsMock,
} = vi.hoisted(() => ({
  projectsMock: [] as string[],
  moveToProjectSpy: vi.fn(),
  deleteProjectSpy: vi.fn(),
  // Server-side "ids in this project" check that gates the remove
  // confirmation. Defaults to "no other sessions"; tests override per case.
  fetchProjectSessionIdsMock: vi.fn(() => Promise.resolve([] as string[])),
  // Latest conversations handed to the global-list mock. The useProjectSessions
  // mock derives each folder's rows from this by label, mirroring the server's
  // ?project= filter — so tests that seed project sessions via the global list
  // keep working without a separate per-project fixture.
  conversationsRef: { current: [] as { id: string; labels?: Record<string, string> }[] },
  // Per-project override: when a test sets projectSessionsMock[name], the folder
  // serves exactly those rows instead of deriving from the global list — used to
  // prove a folder fetches its members independently of the global window.
  projectSessionsMock: { current: {} as Record<string, unknown[]> },
}));

// Mutation hooks are only invoked on row actions; stub them. useConversations
// is the data source under test, so it's a controllable mock.
vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({ mutate: vi.fn() }),
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useStopSession: () => ({ mutate: vi.fn() }),
  // Project feature: the sidebar reads the project list to build project
  // sections, and rows fire useMoveToProject from the kebab menu. Both must
  // be stubbed or the Sidebar throws on render.
  useProjects: () => ({ data: projectsMock }),
  // Each project folder fetches its own sessions (server-side ?project=). Derive
  // them from the global-list fixture by label so existing tests keep seeding
  // project sessions there. Single page, no pagination, in this mock.
  useProjectSessions: (project: string, enabled: boolean) => {
    const override = projectSessionsMock.current[project];
    const rows = !enabled
      ? []
      : (override ??
        conversationsRef.current.filter(
          (c) => (c.labels?.omni_project ?? null) === project && (c as any).archived !== true,
        ));
    return {
      data: enabled
        ? {
            pages: [{ data: rows, first_id: null, last_id: null, has_more: false }],
            pageParams: [undefined],
          }
        : undefined,
      isLoading: false,
      isError: false,
      error: null,
      fetchNextPage: vi.fn(),
      hasNextPage: false,
      isFetchingNextPage: false,
    };
  },
  useMoveToProject: () => ({ mutate: moveToProjectSpy }),
  useDeleteProject: () => ({ mutate: deleteProjectSpy, isPending: false, isError: false }),
  fetchProjectSessionIds: fetchProjectSessionIdsMock,
  PROJECT_LABEL_KEY: "omni_project",
}));
// Header / dialog children that pull their own context — stub to keep the
// test scoped to the conversation list + funnel.
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

function conv(id: string, agentName: string, partial: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    agent_name: agentName,
    ...partial,
  };
}

// Three distinct agent types, mirroring the user's report
// (databricks_coding_agent / Claude Code / Codex).
const THREE_TYPE_CONVERSATIONS = [
  conv("conv_a", "databricks_coding_agent"),
  conv("conv_b", "databricks_coding_agent"),
  conv("conv_c", "Claude Code"),
  conv("conv_d", "Codex"),
];

function mockConversations(convs: Conversation[]) {
  const result = (rows: Conversation[]) =>
    ({
      data: {
        pages: [
          {
            data: rows,
            first_id: rows[0]?.id ?? null,
            last_id: rows.at(-1)?.id ?? null,
            has_more: false,
          },
        ],
        pageParams: [undefined],
      },
      isLoading: false,
      isError: false,
      error: null,
      fetchNextPage: vi.fn(),
      hasNextPage: false,
      isFetchingNextPage: false,
    }) as unknown as ReturnType<typeof useConversations>;
  // The sidebar fetches a single undifferentiated session list.
  conversationsRef.current = convs;
  useConvMock.mockImplementation(() => result(convs));
}

function renderSidebar(open = true, initialEntry = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Sidebar open={open} onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useConvMock.mockReset();
  localStorage.clear();
  projectsMock.length = 0;
  moveToProjectSpy.mockReset();
  deleteProjectSpy.mockReset();
  fetchProjectSessionIdsMock.mockReset();
  fetchProjectSessionIdsMock.mockResolvedValue([]);
  projectSessionsMock.current = {};
});
afterEach(cleanup);

describe("Sidebar session list", () => {
  it("renders no filter funnel and requests the list with archived included", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();

    // The funnel (agent-type filter + "Show archived" toggle) was removed,
    // so its trigger button must be gone entirely.
    expect(screen.queryByRole("button", { name: "Filter sessions" })).toBeNull();

    // The sidebar issues a single session-list query with `includeArchived`
    // hard-wired to true, so archived sessions can be peeled into the
    // bottom "Archived" section. A regression to false would make that
    // section perpetually empty.
    expect(useConvMock.mock.calls).toHaveLength(1);
    expect(useConvMock.mock.calls[0]).toEqual(["", true, { reconcileWhileConnected: true }]);
  });

  it("swaps the card content to the settings section nav on /settings", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar(true, "/settings");

    // The same card now shows the settings nav (Back to app + sections),
    // not the conversation search/list.
    expect(screen.queryByPlaceholderText("Search sessions")).toBeNull();
    expect(screen.getByRole("link", { name: /Back to Omnigent/ })).toHaveAttribute("href", "/");
    expect(screen.getByTestId("settings-nav-appearance")).toHaveAttribute(
      "href",
      "/settings/appearance",
    );
    expect(screen.getByTestId("settings-nav-archived")).toHaveAttribute(
      "href",
      "/settings/archived",
    );
  });

  it("renders the footer Settings as an icon-only floating control on mobile", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();

    const settings = screen.getByTestId("settings-button");
    // Accessible name survives even though the label is visually dropped on
    // mobile (the icon stands alone there).
    expect(settings).toHaveAttribute("aria-label", "Settings");
    // Mobile: compact square icon button, out of flow at the bottom-left.
    expect(settings.className).toContain("max-md:size-9");
    // The text label is desktop-only.
    const label = within(settings).getByText("Settings");
    expect(label.className).toContain("max-md:hidden");
  });

  it("does NOT close the sidebar when the footer Settings is tapped", () => {
    // No onNavClick on the footer Settings link: on mobile the overlay stays
    // open and swaps to the settings section list rather than collapsing onto
    // the default section's content.
    mockConversations(THREE_TYPE_CONVERSATIONS);
    const onClose = vi.fn();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/"]}>
            <Sidebar open onClose={onClose} />
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByTestId("settings-button"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("keeps archived sessions out of the sidebar list (they live on the Settings page)", () => {
    mockConversations([
      conv("conv_active", "Claude Code"),
      conv("conv_archived", "Claude Code", { archived: true }),
    ]);
    renderSidebar();

    // There is no longer an "Archived" section in the sidebar — archived
    // chats are surfaced on /settings, reached via the footer Settings row.
    expect(screen.queryByRole("button", { name: "Archived" })).toBeNull();
    expect(screen.queryByText("conv_archived")).toBeNull();
    // Active sessions still render in Chats.
    const recentSection = screen.getByText("Chats").closest("section")!;
    expect(within(recentSection).getByText("conv_active")).toBeInTheDocument();
    // The footer Settings link points at the settings page.
    expect(screen.getByTestId("settings-button")).toHaveAttribute("href", "/settings");
  });

  it("renders sessions in one flat list with no connection grouping and no Sessions subheader", () => {
    // Liveness grouping is gone: sessions are no longer split into
    // Connected / Disconnected sections. They all land in one flat list with
    // NO "Sessions" subheader (it's the sidebar's baseline list, so the label
    // is redundant). The per-row lifecycle badge still shows for a running
    // session (the badge no longer reflects runner connection state).
    const online = conv("conv_online", "Codex", { status: "running" });
    const offline = conv("conv_offline", "Claude Code", { status: "running" });
    mockConversations([online, offline]);

    renderSidebar();

    // No connection-grouping headings, and no redundant "Sessions" subheader.
    expect(screen.queryByRole("heading", { name: "Connected" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Disconnected" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Sessions" })).toBeNull();

    // Both rows render in the flat list, and the online running session shows
    // its lifecycle badge (in the row's time-marker slot, outside the link).
    expect(screen.getByRole("link", { name: /conv_offline/ })).toBeInTheDocument();
    const onlineRow = screen.getByRole("link", { name: /conv_online/ }).closest("li")!;
    expect(within(onlineRow).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "running",
    );
  });

  it("shows the session-state badge OR the timestamp, never both", () => {
    // Fresh updated_at → relativeTime renders "now", reproducing the
    // reported bug: a status marker AND "now" side by side.
    const freshSeconds = Math.floor(Date.now() / 1000);
    mockConversations([
      conv("conv_working", "Codex", { status: "running", updated_at: freshSeconds }),
      conv("conv_awaiting", "Codex", {
        pending_elicitations_count: 1,
        updated_at: freshSeconds,
      }),
      conv("conv_idle", "Claude Code", { updated_at: freshSeconds }),
    ]);
    renderSidebar();

    // Working row: the running dot takes the time-marker slot and the
    // redundant "now" is suppressed. Both appearing = the either/or rule
    // regressed.
    const workingRow = screen.getByRole("link", { name: /conv_working/ }).closest("li")!;
    expect(within(workingRow).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "running",
    );
    expect(within(workingRow).queryByText("now")).toBeNull();

    // Awaiting row: same rule for the "Needs response" tag — any non-null
    // session state replaces the timestamp, not just the working dot.
    const awaitingRow = screen.getByRole("link", { name: /conv_awaiting/ }).closest("li")!;
    expect(within(awaitingRow).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "awaiting",
    );
    expect(within(awaitingRow).queryByText("now")).toBeNull();

    // Idle row: no badge, so the timestamp must still render — suppressing
    // it everywhere would be an over-broad fix.
    const idleRow = screen.getByRole("link", { name: /conv_idle/ }).closest("li")!;
    expect(within(idleRow).getByText("now")).toBeInTheDocument();
  });
});

// Sidebar grouping: Pinned / Chats / Shared with me are distinguished by
// muted micro-headers + whitespace only (the pink divider rules are gone).
// "Shared with me" = sessions where the caller's permission_level says
// non-owner (< 4); null/4+ are the viewer's own sessions.
describe("Sidebar sections", () => {
  it("splits owned and shared sessions under Chats / Shared with me", () => {
    mockConversations([
      conv("conv_mine_legacy", "Claude Code"), // permission_level null = owner
      conv("conv_mine_acl", "Claude Code", { permission_level: 4 }),
      conv("conv_shared", "Claude Code", { permission_level: 2 }),
    ]);
    renderSidebar();

    // Both headers render because both groups are non-empty.
    const recentHeader = screen.getByText("Chats");
    const sharedHeader = screen.getByText("Shared with me");
    // Each row lands in the right <section>: a mis-split would either leak
    // a shared session into Chats (viewer thinks they own it) or hide an
    // owned one under Shared with me.
    const recentSection = recentHeader.closest("section")!;
    const sharedSection = sharedHeader.closest("section")!;
    expect(within(recentSection).getByText("conv_mine_legacy")).toBeInTheDocument();
    expect(within(recentSection).getByText("conv_mine_acl")).toBeInTheDocument();
    expect(within(recentSection).queryByText("conv_shared")).toBeNull();
    expect(within(sharedSection).getByText("conv_shared")).toBeInTheDocument();
  });

  it("titles the baseline list Chats even with no sibling group", () => {
    mockConversations([conv("conv_only_mine", "Claude Code")]);
    renderSidebar();
    // "Chats" always renders so the list is labeled (and collapsible)
    // from the first session; empty sibling groups stay hidden.
    expect(screen.getByText("conv_only_mine")).toBeInTheDocument();
    expect(screen.getByText("Chats")).toBeInTheDocument();
    expect(screen.queryByText("Shared with me")).toBeNull();
  });
});

// Section headers double as collapse toggles, persisted to localStorage so
// the preference survives reloads (same contract as pins).
describe("Sidebar collapsible sections", () => {
  it("collapses a section on header click and persists across remount", () => {
    mockConversations([
      conv("conv_mine", "Claude Code"),
      conv("conv_shared", "Claude Code", { permission_level: 2 }),
    ]);
    renderSidebar();

    // Collapse hides the section's rows but keeps the header (and the
    // other section untouched) — a vanished header would strand the user
    // with no way to expand again.
    fireEvent.click(screen.getByRole("button", { name: "Shared with me" }));
    expect(screen.queryByText("conv_shared")).toBeNull();
    expect(screen.getByRole("button", { name: "Shared with me" })).toBeInTheDocument();
    expect(screen.getByText("conv_mine")).toBeInTheDocument();

    // Fresh mount re-reads localStorage: still collapsed. If this fails,
    // the toggle wrote state only to memory and reloads lose it.
    cleanup();
    renderSidebar();
    expect(screen.queryByText("conv_shared")).toBeNull();

    // Expanding brings the rows back.
    fireEvent.click(screen.getByRole("button", { name: "Shared with me" }));
    expect(screen.getByText("conv_shared")).toBeInTheDocument();
  });
});

// Pagination belongs to the Chats list: collapsing Chats must take the
// "Load more" button with it, or the button floats under nothing.
describe("Sidebar load-more vs collapsed Chats", () => {
  it("hides Load more while Chats is collapsed and restores it on expand", () => {
    const rows = [conv("conv_mine", "Claude Code")];
    useConvMock.mockImplementation(
      () =>
        ({
          data: {
            pages: [{ data: rows, first_id: rows[0]!.id, last_id: rows[0]!.id, has_more: true }],
            pageParams: [undefined],
          },
          isLoading: false,
          isError: false,
          error: null,
          fetchNextPage: vi.fn(),
          hasNextPage: true,
          isFetchingNextPage: false,
        }) as unknown as ReturnType<typeof useConversations>,
    );
    renderSidebar();

    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Chats" }));
    // Collapsed Chats hides its rows AND the pagination affordance.
    expect(screen.queryByText("conv_mine")).toBeNull();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Chats" }));
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
  });

  it("auto-fetches the next page when the sentinel scrolls into view (infinite scroll)", () => {
    // Capture the IntersectionObserver callback so the test can simulate the
    // sentinel entering the scroll viewport.
    let observerCallback: IntersectionObserverCallback | undefined;
    const observe = vi.fn();
    const disconnect = vi.fn();
    class TestObserver {
      constructor(cb: IntersectionObserverCallback) {
        observerCallback = cb;
      }
      observe = observe;
      unobserve = vi.fn();
      disconnect = disconnect;
      takeRecords = () => [];
      root = null;
      rootMargin = "";
      thresholds = [];
    }
    vi.stubGlobal("IntersectionObserver", TestObserver);

    const fetchNextPage = vi.fn();
    const rows = [conv("conv_mine", "Claude Code")];
    useConvMock.mockImplementation(
      () =>
        ({
          data: {
            pages: [{ data: rows, first_id: rows[0]!.id, last_id: rows[0]!.id, has_more: true }],
            pageParams: [undefined],
          },
          isLoading: false,
          isError: false,
          error: null,
          fetchNextPage,
          hasNextPage: true,
          isFetchingNextPage: false,
        }) as unknown as ReturnType<typeof useConversations>,
    );
    renderSidebar();

    // The sentinel is observed, and nothing is fetched until it intersects.
    expect(observe).toHaveBeenCalledTimes(1);
    expect(fetchNextPage).not.toHaveBeenCalled();

    // Simulate the sentinel leaving view, then entering it.
    observerCallback!([{ isIntersecting: false } as IntersectionObserverEntry], {} as never);
    expect(fetchNextPage).not.toHaveBeenCalled();
    observerCallback!([{ isIntersecting: true } as IntersectionObserverEntry], {} as never);
    expect(fetchNextPage).toHaveBeenCalledTimes(1);

    vi.unstubAllGlobals();
  });
});

// Project feature: sessions carrying a project label are peeled out of
// "Chats" into a folder under the "Projects" group (rendered between Pinned and
// Chats). The project list comes from useProjects() (mocked here).
describe("Sidebar project sections", () => {
  it("groups sessions by their project label, separate from Chats", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_unfiled", "Claude Code"),
      conv("conv_filed", "Claude Code", { labels: { omni_project: "Customer X" } }),
    ]);
    renderSidebar();

    // projects default collapsed, so the row is hidden until the header is
    // clicked. The unfiled session stays visible in Chats regardless.
    const recentSection = screen.getByText("Chats").closest("section")!;
    expect(within(recentSection).getByText("conv_unfiled")).toBeInTheDocument();
    expect(within(recentSection).queryByText("conv_filed")).toBeNull();
    expect(screen.queryByText("conv_filed")).toBeNull();

    // Expanding the project reveals its session under the project section.
    fireEvent.click(screen.getByRole("button", { name: /^Customer X/ }));
    const projectSection = screen.getByText("Customer X").closest("section")!;
    expect(within(projectSection).getByText("conv_filed")).toBeInTheDocument();
    expect(within(recentSection).queryByText("conv_filed")).toBeNull();
  });

  it("fills a folder from its own fetch, independent of the global list window", async () => {
    projectsMock.push("Customer X");
    // The global list holds only an unfiled chat — the project's sessions are
    // on an unloaded global page (the reported bug: folder showed "No chats"
    // until you scrolled). The folder fetches them itself via useProjectSessions.
    mockConversations([conv("conv_unfiled", "Claude Code")]);
    projectSessionsMock.current["Customer X"] = [
      conv("conv_far_1", "Claude Code", { labels: { omni_project: "Customer X" } }),
      conv("conv_far_2", "Claude Code", { labels: { omni_project: "Customer X" } }),
    ];
    renderSidebar();

    // Collapsed by default: rows hidden even though the folder would fetch them.
    expect(screen.queryByText("conv_far_1")).toBeNull();

    // Expanding shows the folder's own members — none of which are in the
    // global list — proving per-folder fetching, not global-window filtering.
    fireEvent.click(screen.getByRole("button", { name: /^Customer X/ }));
    const projectSection = screen.getByText("Customer X").closest("section")!;
    expect(within(projectSection).getByText("conv_far_1")).toBeInTheDocument();
    expect(within(projectSection).getByText("conv_far_2")).toBeInTheDocument();
  });

  it("offers a pencil that starts a new session pre-filed under the project", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_filed", "Claude Code", { labels: { omni_project: "Customer X" } }),
    ]);
    renderSidebar();

    // The pencil links to the landing composer with the project pre-selected
    // via the `?project=` query param (URL-encoded).
    const pencil = screen.getByTestId("project-new-session");
    expect(pencil).toHaveAttribute("aria-label", "New session in Customer X");
    expect(pencil.closest("a")).toHaveAttribute("href", "/?project=Customer%20X");
  });

  it("closes the mobile overlay when the project pencil is tapped", () => {
    // jsdom's matchMedia mock reports non-desktop, so isMobileViewport() is
    // true: a plain pencil tap must close the full-screen sidebar overlay,
    // otherwise the pre-filed new-session page is left hidden behind it.
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_filed", "Claude Code", { labels: { omni_project: "Customer X" } }),
    ]);
    const onClose = vi.fn();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/"]}>
            <Sidebar open onClose={onClose} />
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByTestId("project-new-session").closest("a")!);
    expect(onClose).toHaveBeenCalled();
  });

  it("starts a project folder collapsed with its rows hidden", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_filed", "Claude Code", { labels: { omni_project: "Customer X" } }),
    ]);
    renderSidebar();

    // The folder header is present under the (default-expanded) Projects group,
    // but the folder itself starts collapsed: its row is hidden and the toggle
    // reports collapsed via aria-expanded. Headers carry no count badge.
    const header = screen.getByRole("button", { name: /^Customer X/ });
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("conv_filed")).toBeNull();
  });

  it("auto-expands the project folder holding the selected session", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_filed", "Claude Code", { labels: { omni_project: "Customer X" } }),
    ]);
    // Render with the filed session active (a matched /c/:conversationId route
    // so useParams resolves), instead of the default renderSidebar() which
    // mounts at "/".
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/conv_filed"]}>
            <Routes>
              <Route path="/c/:conversationId" element={<Sidebar open onClose={vi.fn()} />} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );

    // No click: the folder opens because its session is selected, and the row
    // is visible under the project section.
    const header = screen.getByRole("button", { name: /^Customer X/ });
    expect(header).toHaveAttribute("aria-expanded", "true");
    const projectSection = screen.getByText("Customer X").closest("section")!;
    expect(within(projectSection).getByText("conv_filed")).toBeInTheDocument();
  });

  it("moves a pinned project session out into the global Pinned section", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_plain", "Claude Code", { labels: { omni_project: "Customer X" } }),
      conv("conv_pinned", "Claude Code", { labels: { omni_project: "Customer X" } }),
    ]);
    // Pin one of the filed sessions via localStorage (client-side pins).
    localStorage.setItem("omnigent:pinned-conversation-ids", JSON.stringify(["conv_pinned"]));
    renderSidebar();

    // Pinned takes precedence over Project: the pinned session leaves the
    // project and renders in the flat global Pinned section.
    const pinnedSection = screen.getByText("Pinned").closest("section")!;
    expect(within(pinnedSection).getByText("conv_pinned")).toBeInTheDocument();

    // The project folder keeps only its non-pinned session.
    fireEvent.click(screen.getByRole("button", { name: /^Customer X/ }));
    const projectSection = screen.getByText("Customer X").closest("section")!;
    expect(within(projectSection).getByText("conv_plain")).toBeInTheDocument();
    expect(within(projectSection).queryByText("conv_pinned")).toBeNull();
  });

  it("does not render a project section when useProjects returns nothing", () => {
    // A session with a stale project label but no matching project entry stays
    // in Chats — projects are driven by the project list, not the labels alone.
    mockConversations([conv("conv_filed", "Claude Code", { labels: { omni_project: "Ghost" } })]);
    renderSidebar();

    expect(screen.queryByText("Ghost")).toBeNull();
    const recentSection = screen.getByText("Chats").closest("section")!;
    expect(within(recentSection).getByText("conv_filed")).toBeInTheDocument();
  });

  it("collapses all project folders at once and reopens the previously-open set", () => {
    projectsMock.push("Alpha", "Beta");
    mockConversations([
      conv("conv_a", "Claude Code", { labels: { omni_project: "Alpha" } }),
      conv("conv_b", "Claude Code", { labels: { omni_project: "Beta" } }),
    ]);
    renderSidebar();

    // No collapse-all control until at least one folder is open.
    expect(screen.queryByTestId("collapse-all-projects")).toBeNull();

    // Open both folders.
    fireEvent.click(screen.getByRole("button", { name: /^Alpha/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Beta/ }));
    expect(screen.getByRole("button", { name: /^Alpha/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /^Beta/ })).toHaveAttribute("aria-expanded", "true");

    // Collapse all → every folder folds, and the control flips to "reopen".
    fireEvent.click(screen.getByTestId("collapse-all-projects"));
    expect(screen.getByRole("button", { name: /^Alpha/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByRole("button", { name: /^Beta/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("collapse-all-projects")).toBeNull();

    // Reopen previous → restores exactly the set that was open.
    fireEvent.click(screen.getByTestId("reopen-previous-projects"));
    expect(screen.getByRole("button", { name: /^Alpha/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /^Beta/ })).toHaveAttribute("aria-expanded", "true");
  });

  it("deletes a project (and all its sessions) from the folder kebab after confirming", async () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_filed", "Claude Code", { labels: { omni_project: "Customer X" } }),
    ]);
    renderSidebar();

    // Open the project folder's kebab → "Delete project".
    fireEvent.pointerDown(screen.getByRole("button", { name: "Project actions for Customer X" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByTestId("delete-project"));

    // The confirmation makes clear it removes every session, then fires the
    // delete with the project name.
    expect(screen.getByText(/all of its sessions/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));
    expect(deleteProjectSpy).toHaveBeenCalledWith("Customer X", expect.anything());
  });
});

// A collapsed project bubbles up its hidden rows' marker, using the same
// SessionStateBadge a row shows. Only while collapsed.
describe("Sidebar collapsed project marker", () => {
  it("shows the row's session-state badge on a collapsed project", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_awaiting", "Claude Code", {
        labels: { omni_project: "Customer X" },
        pending_elicitations_count: 1,
      }),
    ]);
    renderSidebar();

    // Collapsed by default → the row is hidden, but its "Needs response"
    // marker surfaces on the project header.
    const header = screen.getByRole("button", { name: /^Customer X/ });
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(within(header).getByText("Needs response")).toBeInTheDocument();
  });

  it("drops the header marker once the project is expanded", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_awaiting", "Claude Code", {
        labels: { omni_project: "Customer X" },
        pending_elicitations_count: 1,
      }),
    ]);
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /^Customer X/ }));
    const header = screen.getByRole("button", { name: /^Customer X/ });
    expect(header).toHaveAttribute("aria-expanded", "true");
    // The visible row now owns the badge; the header no longer carries it.
    expect(within(header).queryByText("Needs response")).toBeNull();
  });

  it("shows no header marker when no filed row has one", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_plain", "Claude Code", { labels: { omni_project: "Customer X" } }),
    ]);
    renderSidebar();

    const header = screen.getByRole("button", { name: /^Customer X/ });
    expect(within(header).queryByText("Needs response")).toBeNull();
  });
});

// Every section is expanded by default, but a collapse the user makes
// persists across reloads.
describe("Sidebar default section collapse", () => {
  it("expands Pinned and Chats by default when there is no stored preference", () => {
    localStorage.setItem("omnigent:pinned-conversation-ids", JSON.stringify(["conv_pin"]));
    mockConversations([conv("conv_pin", "Claude Code"), conv("conv_recent", "Claude Code")]);
    renderSidebar();

    expect(screen.getByRole("button", { name: /Pinned/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Chats/ })).toHaveAttribute("aria-expanded", "true");
  });

  it("honors a persisted collapse of Chats across remount", () => {
    localStorage.setItem("omnigent:collapsed-sidebar-sections", JSON.stringify(["Chats"]));
    mockConversations([conv("conv_recent", "Claude Code")]);
    renderSidebar();

    expect(screen.getByRole("button", { name: /Chats/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("conv_recent")).toBeNull();
  });
});

// The quick-pin affordance is hover-revealed on every row — including pinned
// ones. A pinned row no longer keeps a persistent pin marker (the "Pinned"
// section header already conveys the state); on hover it reveals the UNPIN
// control.
describe("Sidebar pin marker visibility", () => {
  it("hover-reveals an unpin control on a pinned row (no persistent marker)", () => {
    mockConversations([conv("conv_pin", "Claude Code")]);
    localStorage.setItem("omnigent:pinned-conversation-ids", JSON.stringify(["conv_pin"]));
    renderSidebar();

    const pinned = screen.getByText("Pinned").closest("section")!;
    const pinButton = within(pinned).getByTestId("quick-pin-conversation");
    // Hover-gated like every other row (no persistent opacity-100 marker), and
    // the control unpins.
    expect(pinButton.className).toContain("md:opacity-0");
    expect(pinButton).toHaveAttribute("aria-label", "Unpin conversation");
  });

  it("hides the pin affordance until hover on an unpinned row", () => {
    mockConversations([conv("conv_plain", "Claude Code")]);
    renderSidebar();

    const pinButton = screen.getByTestId("quick-pin-conversation");
    // Unpinned: hover-gated reveal (opacity-0 until group-hover).
    expect(pinButton.className).toContain("md:opacity-0");
  });
});

// The kebab menu's "Change project" item opens the project picker; selecting a
// project fires useMoveToProject with the row id and chosen project name.
describe("Sidebar move-to-project action", () => {
  it("moves a session into a project selected from the picker", async () => {
    projectsMock.push("Sprint 42");
    mockConversations([conv("conv_move", "Claude Code")]);
    renderSidebar();

    // Open the row's kebab menu (Radix opens on pointerdown, not click), then
    // open the "Change project" submenu flyout.
    const row = screen.getByRole("link", { name: /conv_move/ }).closest("li")!;
    fireEvent.pointerDown(within(row).getByRole("button", { name: "Conversation actions" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByTestId("move-to-project"));

    // projects render as menu items inside the submenu; picking one fires the
    // mutation with id + project.
    fireEvent.click(await screen.findByRole("menuitem", { name: /Sprint 42/ }));
    expect(moveToProjectSpy).toHaveBeenCalledWith({ id: "conv_move", project: "Sprint 42" });
  });

  it("confirms removal only when it's the project's last session", async () => {
    projectsMock.push("Sprint 42");
    mockConversations([
      conv("conv_filed", "Claude Code", { labels: { omni_project: "Sprint 42" } }),
    ]);
    // Server reports this is the only session in the project.
    fetchProjectSessionIdsMock.mockResolvedValue(["conv_filed"]);
    renderSidebar();

    // Expand the project folder, open the filed row's kebab → Change project.
    fireEvent.click(screen.getByRole("button", { name: "Sprint 42" }));
    const row = screen.getByRole("link", { name: /conv_filed/ }).closest("li")!;
    fireEvent.pointerDown(within(row).getByRole("button", { name: "Conversation actions" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByTestId("move-to-project"));

    // Last session → "Remove from <project>" opens a confirmation that says the
    // project will be removed too; it does NOT remove immediately.
    fireEvent.click(await screen.findByRole("menuitem", { name: /Remove from Sprint 42/ }));
    expect(await screen.findByText(/the project will be removed as well/i)).toBeInTheDocument();
    expect(moveToProjectSpy).not.toHaveBeenCalled();

    // Confirming fires the removal with an empty project (server deletes the
    // label; the implicit project vanishes with its last session).
    fireEvent.click(screen.getByRole("button", { name: "Remove from project" }));
    expect(moveToProjectSpy).toHaveBeenCalledWith(
      { id: "conv_filed", project: "" },
      expect.anything(),
    );
  });

  it("removes without confirmation when other sessions remain in the project", async () => {
    projectsMock.push("Sprint 42");
    mockConversations([
      conv("conv_filed", "Claude Code", { labels: { omni_project: "Sprint 42" } }),
    ]);
    // Server reports another session is still in the project.
    fetchProjectSessionIdsMock.mockResolvedValue(["conv_filed", "conv_other"]);
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: "Sprint 42" }));
    const row = screen.getByRole("link", { name: /conv_filed/ }).closest("li")!;
    fireEvent.pointerDown(within(row).getByRole("button", { name: "Conversation actions" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByTestId("move-to-project"));
    fireEvent.click(await screen.findByRole("menuitem", { name: /Remove from Sprint 42/ }));

    // Not the last session → removes straight away, no confirmation dialog.
    await waitFor(() =>
      expect(moveToProjectSpy).toHaveBeenCalledWith({ id: "conv_filed", project: "" }),
    );
    expect(screen.queryByText(/the project will be removed as well/i)).toBeNull();
  });
});

describe("Sidebar mobile overlay background", () => {
  it("keeps the opaque bg-card-solid override for the mobile full-screen overlay", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();

    const aside = screen.getByRole("complementary", { name: "Conversations" });
    // On mobile the sidebar is a fixed full-screen overlay ON TOP of the
    // chat. Its desktop look uses the translucent glass --card (60% alpha
    // in dark mode) + backdrop blur, but WebKit/Safari drops the blur as
    // soon as a Radix popper (the row kebab menu) opens — and never
    // repaints it — so the chat bled through the overlay. The fix pins an
    // opaque background below the md breakpoint. If this assertion fails,
    // the override was removed and the Safari mobile bleed-through is back.
    expect(aside.className).toContain("max-md:bg-card-solid");
    // Desktop keeps the glass treatment: base bg-card must stay alongside
    // the mobile override (removing it would kill the desktop frosted look).
    expect(aside.className).toMatch(/(^| )bg-card( |$)/);
  });
});

// When the active conversation changes (e.g. a freshly created session the
// app navigates to via /c/:id), its sidebar row scrolls into view so it isn't
// stranded below the fold. We center it with a smooth animation. jsdom doesn't
// implement scrollIntoView, so it's spied on.
describe("Sidebar active-row auto-scroll", () => {
  function renderAtRoute(initialEntry: string) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={[initialEntry]}>
            <Routes>
              <Route path="/" element={<Sidebar open onClose={vi.fn()} />} />
              <Route path="/c/:conversationId" element={<Sidebar open onClose={vi.fn()} />} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );
  }

  it("scrolls the active session's row to center with a smooth animation", () => {
    const scrollIntoView = vi.fn();
    vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(scrollIntoView);

    mockConversations([conv("conv_top", "Claude Code"), conv("conv_active", "Claude Code")]);
    renderAtRoute("/c/conv_active");

    // The active row owns the only scrollIntoView call, centered + smooth.
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });

    vi.restoreAllMocks();
  });

  it("does not scroll any row when no conversation is active", () => {
    const scrollIntoView = vi.fn();
    vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(scrollIntoView);

    mockConversations([conv("conv_a", "Claude Code"), conv("conv_b", "Claude Code")]);
    // Landing route "/" has no :conversationId — nothing is active, so no row
    // should yank the list around on mount.
    renderAtRoute("/");

    expect(scrollIntoView).not.toHaveBeenCalled();

    vi.restoreAllMocks();
  });
});

describe("Sidebar collapsed marker", () => {
  // The dark-mode glass rule in index.css keys its border/blur on
  // :not([data-collapsed]) — NOT on aria-hidden, which Radix also toggles
  // on the open sidebar while a modal menu is up (that coupling made every
  // row reflow 2px wider when the session kebab menu opened). The panel
  // must set data-collapsed exactly when closed; index.css.test.ts pins
  // the selector side of this contract.
  it("sets data-collapsed only while closed", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    // Closed panels are aria-hidden, which strips their accessible name —
    // the role+name query can't reach them, so select by class instead.
    const { container } = renderSidebar(false);
    const aside = container.querySelector("aside.conversations-sidebar")!;
    // Closed: marked collapsed so the glass rule skips the w-0 strip.
    expect(aside).toHaveAttribute("data-collapsed");
    cleanup();

    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar(true);
    const openAside = screen.getByRole("complementary", { name: "Conversations" });
    // Open: the attribute must be ABSENT — rendering it as "false" would
    // still match [data-collapsed] and strip the glass border while open.
    expect(openAside).not.toHaveAttribute("data-collapsed");
  });
});
