// Integration tests for the Sidebar's session list. The search box no
// longer carries a filter funnel (agent-type filter + "Show archived"
// toggle were removed). The sidebar fetches a single session list with
// archived sessions included, rendering them as grouped sections (Pinned /
// Recent / Shared with me / Archived).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";

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
}));
// Header / dialog children that pull their own context — stub to keep the
// test scoped to the conversation list + funnel.
vi.mock("@/components/theme/ThemeModeMenu", () => ({ ThemeModeMenu: () => null }));
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
  useConvMock.mockImplementation(() => result(convs));
}

function renderSidebar(open = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Sidebar open={open} onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useConvMock.mockReset();
  localStorage.clear();
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

  it("groups archived sessions under an 'Archived' section, separate from Recent", () => {
    mockConversations([
      conv("conv_active", "Claude Code"),
      conv("conv_archived", "Claude Code", { archived: true }),
    ]);
    renderSidebar();

    // Archived starts collapsed by default; expand it to reach its rows.
    fireEvent.click(screen.getByRole("button", { name: "Archived" }));

    // The archived row lands in its own "Archived" <section>, not Recent —
    // mixing it into Recent would defeat the grouping.
    const archivedSection = screen.getByText("Archived").closest("section")!;
    const recentSection = screen.getByText("Recent").closest("section")!;
    expect(within(archivedSection).getByText("conv_archived")).toBeInTheDocument();
    expect(within(recentSection).queryByText("conv_archived")).toBeNull();
    expect(within(recentSection).getByText("conv_active")).toBeInTheDocument();
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

// Sidebar grouping: Pinned / Recent / Shared with me are distinguished by
// muted micro-headers + whitespace only (the pink divider rules are gone).
// "Shared with me" = sessions where the caller's permission_level says
// non-owner (< 4); null/4+ are the viewer's own sessions.
describe("Sidebar sections", () => {
  it("splits owned and shared sessions under Recent / Shared with me", () => {
    mockConversations([
      conv("conv_mine_legacy", "Claude Code"), // permission_level null = owner
      conv("conv_mine_acl", "Claude Code", { permission_level: 4 }),
      conv("conv_shared", "Claude Code", { permission_level: 2 }),
    ]);
    renderSidebar();

    // Both headers render because both groups are non-empty.
    const recentHeader = screen.getByText("Recent");
    const sharedHeader = screen.getByText("Shared with me");
    // Each row lands in the right <section>: a mis-split would either leak
    // a shared session into Recent (viewer thinks they own it) or hide an
    // owned one under Shared with me.
    const recentSection = recentHeader.closest("section")!;
    const sharedSection = sharedHeader.closest("section")!;
    expect(within(recentSection).getByText("conv_mine_legacy")).toBeInTheDocument();
    expect(within(recentSection).getByText("conv_mine_acl")).toBeInTheDocument();
    expect(within(recentSection).queryByText("conv_shared")).toBeNull();
    expect(within(sharedSection).getByText("conv_shared")).toBeInTheDocument();
  });

  it("titles the baseline list Recent even with no sibling group", () => {
    mockConversations([conv("conv_only_mine", "Claude Code")]);
    renderSidebar();
    // "Recent" always renders so the list is labeled (and collapsible)
    // from the first session; empty sibling groups stay hidden.
    expect(screen.getByText("conv_only_mine")).toBeInTheDocument();
    expect(screen.getByText("Recent")).toBeInTheDocument();
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

// Pagination belongs to the Recent list: collapsing Recent must take the
// "Load more" button with it, or the button floats under nothing.
describe("Sidebar load-more vs collapsed Recent", () => {
  it("hides Load more while Recent is collapsed and restores it on expand", () => {
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
    fireEvent.click(screen.getByRole("button", { name: "Recent" }));
    // Collapsed Recent hides its rows AND the pagination affordance.
    expect(screen.queryByText("conv_mine")).toBeNull();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Recent" }));
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
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
