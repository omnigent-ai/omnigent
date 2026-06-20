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

// Collection mocks are declared via vi.hoisted so they exist before the hoisted
// vi.mock factory runs. collectionsMock is mutated per-test to drive collection
// sections; moveToCollectionSpy captures kebab-menu "Move to collection" calls.
const { collectionsMock, moveToCollectionSpy } = vi.hoisted(() => ({
  collectionsMock: [] as string[],
  moveToCollectionSpy: vi.fn(),
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
  // Collection feature: the sidebar reads the collection list to build collection
  // sections, and rows fire useMoveToCollection from the kebab menu. Both must
  // be stubbed or the Sidebar throws on render.
  useCollections: () => ({ data: collectionsMock }),
  useMoveToCollection: () => ({ mutate: moveToCollectionSpy }),
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
  collectionsMock.length = 0;
  moveToCollectionSpy.mockReset();
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

// Collection feature: sessions carrying a `collection` label are peeled out of
// "Recent" into a dedicated section named after the collection, rendered between
// Pinned and Recent. The collection list comes from useCollections() (mocked here).
describe("Sidebar collection sections", () => {
  it("groups sessions by their collection label, separate from Recent", () => {
    collectionsMock.push("Customer X");
    mockConversations([
      conv("conv_unfiled", "Claude Code"),
      conv("conv_filed", "Claude Code", { labels: { collection: "Customer X" } }),
    ]);
    renderSidebar();

    // Collections default collapsed, so the row is hidden until the header is
    // clicked. The unfiled session stays visible in Recent regardless.
    const recentSection = screen.getByText("Recent").closest("section")!;
    expect(within(recentSection).getByText("conv_unfiled")).toBeInTheDocument();
    expect(within(recentSection).queryByText("conv_filed")).toBeNull();
    expect(screen.queryByText("conv_filed")).toBeNull();

    // Expanding the collection reveals its session under the collection section.
    fireEvent.click(screen.getByRole("button", { name: /Customer X/ }));
    const collectionSection = screen.getByText("Customer X").closest("section")!;
    expect(within(collectionSection).getByText("conv_filed")).toBeInTheDocument();
    expect(within(recentSection).queryByText("conv_filed")).toBeNull();
  });

  it("starts collections collapsed and shows their session count", () => {
    collectionsMock.push("Customer X");
    mockConversations([
      conv("conv_filed", "Claude Code", { labels: { collection: "Customer X" } }),
    ]);
    renderSidebar();

    // Header is present (with the rendered count) but the row is hidden, and
    // the toggle reports collapsed via aria-expanded.
    const header = screen.getByRole("button", { name: /Customer X/ });
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(within(header).getByText("1")).toBeInTheDocument();
    expect(screen.queryByText("conv_filed")).toBeNull();
  });

  it("keeps a pinned session inside its collection, sorted first", () => {
    collectionsMock.push("Customer X");
    mockConversations([
      conv("conv_plain", "Claude Code", { labels: { collection: "Customer X" } }),
      conv("conv_pinned", "Claude Code", { labels: { collection: "Customer X" } }),
    ]);
    // Pin one of the collectioned sessions via localStorage (client-side pins).
    localStorage.setItem("omnigent:pinned-conversation-ids", JSON.stringify(["conv_pinned"]));
    renderSidebar();

    // No global "Pinned" section — the pinned session stays in its collection.
    expect(screen.queryByText("Pinned")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Customer X/ }));
    const collectionSection = screen.getByText("Customer X").closest("section")!;
    const rows = within(collectionSection).getAllByRole("link");
    // Pinned row sorts to the top of the collection.
    expect(rows[0]).toHaveTextContent("conv_pinned");
    expect(rows[1]).toHaveTextContent("conv_plain");
  });

  it("does not render a collection section when useCollections returns nothing", () => {
    // A session with a stale collection label but no matching collection entry stays
    // in Recent — collections are driven by the collection list, not the labels alone.
    mockConversations([conv("conv_filed", "Claude Code", { labels: { collection: "Ghost" } })]);
    renderSidebar();

    expect(screen.queryByText("Ghost")).toBeNull();
    const recentSection = screen.getByText("Recent").closest("section")!;
    expect(within(recentSection).getByText("conv_filed")).toBeInTheDocument();
  });
});

// A collapsed collection bubbles up its hidden rows' marker, using the same
// SessionStateBadge a row shows. Only while collapsed.
describe("Sidebar collapsed collection marker", () => {
  it("shows the row's session-state badge on a collapsed collection", () => {
    collectionsMock.push("Customer X");
    mockConversations([
      conv("conv_awaiting", "Claude Code", {
        labels: { collection: "Customer X" },
        pending_elicitations_count: 1,
      }),
    ]);
    renderSidebar();

    // Collapsed by default → the row is hidden, but its "Needs response"
    // marker surfaces on the collection header.
    const header = screen.getByRole("button", { name: /Customer X/ });
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(within(header).getByText("Needs response")).toBeInTheDocument();
  });

  it("drops the header marker once the collection is expanded", () => {
    collectionsMock.push("Customer X");
    mockConversations([
      conv("conv_awaiting", "Claude Code", {
        labels: { collection: "Customer X" },
        pending_elicitations_count: 1,
      }),
    ]);
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /Customer X/ }));
    const header = screen.getByRole("button", { name: /Customer X/ });
    expect(header).toHaveAttribute("aria-expanded", "true");
    // The visible row now owns the badge; the header no longer carries it.
    expect(within(header).queryByText("Needs response")).toBeNull();
  });

  it("shows no header marker when no collectioned row has one", () => {
    collectionsMock.push("Customer X");
    mockConversations([
      conv("conv_plain", "Claude Code", { labels: { collection: "Customer X" } }),
    ]);
    renderSidebar();

    const header = screen.getByRole("button", { name: /Customer X/ });
    expect(within(header).queryByText("Needs response")).toBeNull();
  });
});

// Pinned and Recent are expanded by default (only Archived starts collapsed),
// but a collapse the user makes persists across reloads.
describe("Sidebar default section collapse", () => {
  it("expands Pinned and Recent by default when there is no stored preference", () => {
    localStorage.setItem("omnigent:pinned-conversation-ids", JSON.stringify(["conv_pin"]));
    mockConversations([conv("conv_pin", "Claude Code"), conv("conv_recent", "Claude Code")]);
    renderSidebar();

    expect(screen.getByRole("button", { name: /Pinned/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Recent/ })).toHaveAttribute("aria-expanded", "true");
  });

  it("honors a persisted collapse of Recent across remount", () => {
    localStorage.setItem("omnigent:collapsed-sidebar-sections", JSON.stringify(["Recent"]));
    mockConversations([conv("conv_recent", "Claude Code")]);
    renderSidebar();

    expect(screen.getByRole("button", { name: /Recent/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText("conv_recent")).toBeNull();
  });
});

// The quick-pin affordance is hover-revealed on unpinned rows, but a pinned
// row must keep its pin marker visible at rest so the pinned state reads
// without hovering.
describe("Sidebar pin marker visibility", () => {
  it("keeps the pin button visible at rest once a conversation is pinned", () => {
    mockConversations([conv("conv_pin", "Claude Code")]);
    localStorage.setItem("omnigent:pinned-conversation-ids", JSON.stringify(["conv_pin"]));
    renderSidebar();

    const pinned = screen.getByText("Pinned").closest("section")!;
    const pinButton = within(pinned).getByTestId("quick-pin-conversation");
    // Pinned: always opaque (no hover-gated opacity-0).
    expect(pinButton.className).toContain("opacity-100");
    expect(pinButton.className).not.toContain("md:opacity-0");
  });

  it("omits the pin affordance entirely on an archived row", () => {
    // Pinned in storage AND archived: archive wins, so the row sits in the
    // Archived group with NO pin button at all (not even on hover) — pinning
    // is meaningless there.
    mockConversations([conv("conv_pa", "Claude Code", { archived: true })]);
    localStorage.setItem("omnigent:pinned-conversation-ids", JSON.stringify(["conv_pa"]));
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: "Archived" }));
    const archived = screen.getByText("Archived").closest("section")!;
    expect(within(archived).queryByTestId("quick-pin-conversation")).toBeNull();
  });

  it("hides the pin affordance until hover on an unpinned row", () => {
    mockConversations([conv("conv_plain", "Claude Code")]);
    renderSidebar();

    const pinButton = screen.getByTestId("quick-pin-conversation");
    // Unpinned: hover-gated reveal (opacity-0 until group-hover).
    expect(pinButton.className).toContain("md:opacity-0");
  });
});

// The kebab menu's "Move to collection" item opens the collection picker; selecting a
// collection fires useMoveToCollection with the row id and chosen collection name.
describe("Sidebar move-to-collection action", () => {
  it("moves a session into a collection selected from the picker", async () => {
    collectionsMock.push("Sprint 42");
    mockConversations([conv("conv_move", "Claude Code")]);
    renderSidebar();

    // Open the row's kebab menu (Radix opens on pointerdown, not click), then
    // open the "Move to collection" submenu flyout.
    const row = screen.getByRole("link", { name: /conv_move/ }).closest("li")!;
    fireEvent.pointerDown(within(row).getByRole("button", { name: "Conversation actions" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByTestId("move-to-collection"));

    // Collections render as menu items inside the submenu; picking one fires the
    // mutation with id + collection.
    fireEvent.click(await screen.findByRole("menuitem", { name: /Sprint 42/ }));
    expect(moveToCollectionSpy).toHaveBeenCalledWith({ id: "conv_move", collection: "Sprint 42" });
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
