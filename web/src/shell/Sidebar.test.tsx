import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
vi.mock("@/components/icons/ClaudeIcon", () => ({
  ClaudeIcon: () => <svg data-testid="claude-glyph" />,
}));
vi.mock("@/components/icons/CodexIcon", () => ({
  CodexIcon: () => <svg data-testid="codex-glyph" />,
}));
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { sidebarConfig, type SidebarConfig } from "@/lib/sidebarConfig";
// Integration tests for the Sidebar's session list. The search box no
// longer carries a filter funnel (agent-type filter + "Show archived"
// toggle were removed). The sidebar fetches a single session list with
// archived sessions included, rendering the non-archived ones as grouped
// sections (Pinned / Projects / Sessions / Shared with me). Archived sessions
// are no longer listed here — they live on the Settings page.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useEffect } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ALT_KEY, ARIA_MOD_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { AUTO_HARNESS_ID, SMART_ROUTING_LABEL } from "@/lib/agentLabels";
import { buildComposerSessionDescriptor, composerModelChipLabel } from "@/lib/composerModelLabel";
import {
  type ConversationsInfiniteData,
  mergeItemsIntoPages,
  nullsToUndefined,
} from "@/lib/sessionListCache";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type * as HostsModule from "@/hooks/useHosts";
import {
  markConversationSeen,
  resetReadStateForTests,
  seedReadState,
} from "@/hooks/useUnseenConversations";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { clearOptimisticTitles, recordOptimisticTitle } from "@/lib/optimisticTitles";
import { clearSessionDrafts, setSessionDraft } from "@/lib/sessionDrafts";
import * as sessionsApi from "@/lib/sessionsApi";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { ExtensionCatalogProvider } from "@/extensions/ExtensionProvider";
import type { ExtensionCatalogItem } from "@/extensions/types";

// Project mocks are declared via vi.hoisted so they exist before the hoisted
// vi.mock factory runs. projectsMock is mutated per-test to drive project
// sections; moveToProjectSpy captures kebab-menu "Change project" calls.
const {
  projectsMock,
  projectRowsRef,
  moveToProjectSpy,
  deleteProjectSpy,
  renameProjectSpy,
  createProjectSpy,
  fetchProjectSessionIdsMock,
  conversationsRef,
  pinnedIdsRef,
  projectSessionsMock,
  useHostsMock,
} = vi.hoisted(() => ({
  projectsMock: [] as string[],
  projectRowsRef: { current: undefined as { id: string; name: string }[] | undefined },
  moveToProjectSpy: vi.fn(),
  deleteProjectSpy: vi.fn(),
  renameProjectSpy: vi.fn(),
  createProjectSpy: vi.fn(),
  // Stub for the exported fetchProjectSessionIds helper (kept so the module
  // mock stays complete). Remove-from-project no longer gates on a
  // last-session check, so nothing in these tests depends on its value.
  fetchProjectSessionIdsMock: vi.fn(() => Promise.resolve([] as string[])),
  // Latest conversations handed to the global-list mock. The useProjectSessions
  // mock derives each folder's rows from this by label, mirroring the server's
  // ?project= filter — so tests that seed project sessions via the global list
  // keep working without a separate per-project fixture.
  conversationsRef: {
    current: [] as { id: string; labels?: Record<string, string>; archived?: boolean }[],
  },
  // Server-authoritative pinned ids. Kept in a ref (not the legacy localStorage
  // key, which the one-time migration clears on mount) so a seeded pin survives
  // the first render. `seedPins` sets it; the toggle mock mutates it.
  pinnedIdsRef: { current: [] as string[] },
  // Per-project override: when a test sets projectSessionsMock[name], the folder
  // serves exactly those rows instead of deriving from the global list — used to
  // prove a folder fetches its members independently of the global window.
  projectSessionsMock: { current: {} as Record<string, unknown[]> },
  useHostsMock: vi.fn(),
}));

vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof HostsModule>()),
  useHosts: useHostsMock,
}));

// Mutation hooks are only invoked on row actions; stub them. useConversations
// is the data source under test, so it's a controllable mock.
vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    usePinnedConversations: () => {
      const idSet = new Set(pinnedIdsRef.current);
      return {
        data: {
          conversations: conversationsRef.current.filter((c) => idSet.has(c.id)),
          filterHonored: true,
        },
        isSuccess: true,
      };
    },
    useTogglePinnedConversation: () => ({
      mutate: ({ id, pinned }: { id: string; pinned: boolean }) => {
        const ids = pinnedIdsRef.current;
        pinnedIdsRef.current = pinned
          ? [id, ...ids.filter((x) => x !== id)]
          : ids.filter((x) => x !== id);
      },
    }),
    useProjects: () => ({
      data:
        projectRowsRef.current ?? projectsMock.map((name: string) => ({ id: `p_${name}`, name })),
    }),
    useProjectSessions: (project: string, enabled: boolean) => {
      const override = projectSessionsMock.current[project];
      const rows = !enabled
        ? []
        : (override ??
          conversationsRef.current.filter(
            (c) => (c.labels?.omni_project ?? null) === project && c.archived !== true,
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
    useRenameProject: () => ({ mutate: renameProjectSpy, isPending: false, isError: false }),
    useCreateProject: () => ({ mutate: createProjectSpy, isPending: false, isError: false }),
    fetchProjectSessionIds: fetchProjectSessionIdsMock,
  };
});
// Header / dialog children that pull their own context — stub to keep the
// test scoped to the conversation list + funnel.
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

// The "Shared with me" tab only renders on a multi-user (non-local) server.
// jsdom's default origin is loopback, which would read as single-user and hide
// the tabs; force multi-user so the tab-based tests exercise the split. The
// single-user case (tabs hidden) is covered explicitly below.
const isServerLocalMock = vi.hoisted(() => vi.fn(() => false));
vi.mock("@/lib/serverOrigin", () => ({
  isCurrentServerLocal: isServerLocalMock,
  isLocalServerOrigin: (origin: string) =>
    ["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"].includes(new URL(origin).hostname),
}));

import { useConversations } from "@/hooks/useConversations";
import { useChatStore } from "@/store/chatStore";
import { Sidebar } from "./Sidebar";
import claudeCodeLogo from "@/assets/claude-code-logo.svg";
import * as identity from "@/lib/identity";

const agentsRef = vi.hoisted(() => ({
  current: [] as AvailableAgent[],
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({ data: agentsRef.current }),
}));

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

function renderSidebar(
  open = true,
  initialEntry = "/",
  onOpenSearch?: () => void,
  info?: ServerInfo,
  extensions: ExtensionCatalogItem[] = [],
  onClose = vi.fn(),
  config: SidebarConfig = sidebarConfig,
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const sidebar = <Sidebar open={open} onClose={onClose} onOpenSearch={onOpenSearch} />;
  return render(
    <QueryClientProvider client={qc}>
      <SidebarDataProvider config={config}>
        <ExtensionCatalogProvider extensions={extensions}>
          <TooltipProvider>
            <MemoryRouter initialEntries={[initialEntry]}>
              {info ? <CapabilitiesProvider info={info}>{sidebar}</CapabilitiesProvider> : sidebar}
            </MemoryRouter>
          </TooltipProvider>
        </ExtensionCatalogProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

// Session scope lives in the Sessions heading's filter menu; pick an option to
// switch the slice the list shows (the default filter is "All sessions").
function selectSessionFilter(value: "all" | "mine" | "shared" | "archived") {
  fireEvent.pointerDown(screen.getByTestId("session-filter"), {
    button: 0,
    ctrlKey: false,
    pointerType: "mouse",
  });
  fireEvent.click(screen.getByTestId(`session-filter-${value}`));
}

/** Show only sessions others shared with the viewer. */
function showSharedTab() {
  selectSessionFilter("shared");
}

/** Open the Projects header kebab (expand-all / revert / select sessions). */
function openProjectsMenu() {
  fireEvent.pointerDown(screen.getByRole("button", { name: "Project list actions" }), {
    button: 0,
    ctrlKey: false,
  });
}

/** Close an open dropdown menu (Radix marks the rest of the tree aria-hidden
    while open, so folder buttons aren't queryable until it closes). */
function closeProjectsMenu() {
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
}

beforeEach(() => {
  agentsRef.current = [];
  useConvMock.mockReset();
  useHostsMock.mockReset();
  useHostsMock.mockReturnValue({ data: [] });
  localStorage.clear();
  resetReadStateForTests();
  clearSessionDrafts();
  clearOptimisticTitles();
  projectsMock.length = 0;
  projectRowsRef.current = undefined;
  moveToProjectSpy.mockReset();
  deleteProjectSpy.mockReset();
  fetchProjectSessionIdsMock.mockReset();
  fetchProjectSessionIdsMock.mockResolvedValue([]);
  projectSessionsMock.current = {};
  pinnedIdsRef.current = [];
  // Default to a multi-user server so the tab-based tests see the tabs.
  isServerLocalMock.mockReturnValue(false);
  // The bound session's startup signal (send in flight / PTY pending) feeds
  // the row's "starting" badge; reset so one test's state can't leak.
  useChatStore.setState({ conversationId: null, status: "idle", terminalPending: false });
});

/** Seed the server-authoritative pinned set (replaces the old localStorage seed). */
function seedPins(ids: string[]) {
  pinnedIdsRef.current = ids;
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const TEST_EXTENSION: ExtensionCatalogItem = {
  object: "extension",
  id: "acme.review",
  display_name: "Review",
  distribution: "acme-review",
  version: "1.0.0",
  extension_api: 1,
  status: "enabled",
  permissions: [],
  pages: [
    {
      id: "acme.review.inbox-page",
      title: "Extension Inbox",
      route: "inbox",
      view: "inbox",
    },
  ],
  primary_navigation: [
    {
      id: "acme.review.primary-nav",
      label: "Extension Inbox",
      page: "acme.review.inbox-page",
      icon: "puzzle",
      order: 500,
      when: null,
    },
  ],
  browser: {
    declared: true,
    has_styles: false,
    digest: "digest",
    script_url: "/script",
    style_url: null,
  },
};

describe("Sidebar scroll divider", () => {
  it("separates fixed navigation only while sessions are scrolled", () => {
    mockConversations([conv("session-1", "A session")]);
    renderSidebar();

    const divider = screen.getByTestId("sidebar-scroll-divider");
    const scrollContainer = screen.getByRole("navigation");

    expect(divider).toHaveClass("opacity-0", "absolute", "pointer-events-none");
    expect(divider).toHaveAttribute("aria-hidden", "true");
    expect(scrollContainer).not.toContainElement(divider);

    fireEvent.scroll(scrollContainer, { target: { scrollTop: 1 } });
    expect(divider).toHaveClass("opacity-100");

    fireEvent.scroll(scrollContainer, { target: { scrollTop: 100 } });
    expect(divider).toHaveClass("opacity-100");

    fireEvent.scroll(scrollContainer, { target: { scrollTop: 0 } });
    expect(divider).toHaveClass("opacity-0");

    fireEvent.scroll(scrollContainer, { target: { scrollTop: -10 } });
    expect(divider).toHaveClass("opacity-0");
  });

  it("resets when returning from settings to a fresh session list", () => {
    mockConversations([conv("session-1", "A session")]);
    renderSidebar();

    fireEvent.scroll(screen.getByRole("navigation"), { target: { scrollTop: 100 } });
    expect(screen.getByTestId("sidebar-scroll-divider")).toHaveClass("opacity-100");

    fireEvent.click(screen.getByTestId("sidebar-settings-float"));
    expect(screen.queryByTestId("sidebar-scroll-divider")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: /back/i }));
    expect(screen.getByTestId("sidebar-scroll-divider")).toHaveClass("opacity-0");
  });
});

describe("Sidebar session list", () => {
  it.each([null, 1, 2, 3, 4])(
    "marks shared sessions regardless of permission level %s",
    (level) => {
      mockConversations([
        conv("shared_session", "Claude Code", {
          owner: "other@example.com",
          permission_level: level,
        }),
        conv("private_session", "Claude Code"),
      ]);
      renderSidebar();
      selectSessionFilter("all");

      const sharedRow = screen.getByText("shared_session").closest("li")!;
      const indicator = within(sharedRow).getByRole("img", { name: "Shared session" });
      expect(indicator).toHaveAttribute("title", "Shared with you");
      expect(indicator).toHaveClass("w-6", "justify-center");
      expect(indicator).toHaveClass("absolute", "right-1");
      expect(
        within(screen.getByText("private_session").closest("li")!).queryByRole("img"),
      ).toBeNull();
    },
  );

  it("keeps the session state rightmost when a shared icon is also present", () => {
    mockConversations([
      conv("shared_running", "Claude Code", {
        owner: "other@example.com",
        status: "running",
      }),
    ]);
    renderSidebar();
    selectSessionFilter("all");

    const row = screen.getByText("shared_running").closest("li")!;
    expect(within(row).getByRole("img", { name: "Shared session" })).toHaveClass("right-8");
    expect(within(row).getByTestId("session-state-badge").parentElement).toHaveClass("right-1");
  });

  it("does not mark the viewer's own sessions or sessions without ownership metadata", () => {
    const viewer = vi.spyOn(identity, "getCurrentUserId").mockReturnValue("viewer@example.com");
    try {
      mockConversations([
        conv("owned_session", "Claude Code", { owner: "viewer@example.com" }),
        conv("null_owner", "Claude Code", { owner: null }),
        conv("missing_owner", "Claude Code"),
      ]);
      renderSidebar();
      expect(screen.queryByRole("img", { name: "Shared session" })).toBeNull();
    } finally {
      viewer.mockRestore();
    }
  });

  it("keeps the shared indicator on pinned sessions across filters", () => {
    mockConversations([conv("shared_pin", "Claude Code", { owner: "other@example.com" })]);
    seedPins(["shared_pin"]);
    renderSidebar();

    for (const filter of ["mine", "shared", "all", "archived"] as const) {
      selectSessionFilter(filter);
      const pinned = screen.getByText("Pinned").closest("section")!;
      expect(within(pinned).getByRole("img", { name: "Shared session" })).toBeInTheDocument();
    }
  });

  it("uses the interface text token for the empty session-list state", () => {
    mockConversations([]);
    renderSidebar();

    expect(screen.getByText("No sessions")).toHaveClass("text-ui");
    expect(screen.getByText("No sessions")).not.toHaveClass("text-sm");
  });

  it("flips a just-created session's row to its provisional first-prompt label", () => {
    mockConversations([
      conv("conv_opt", "Claude Code", {
        title: null,
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
      }),
    ]);
    renderSidebar();

    // Before the landing form's stash lands (and for sessions born
    // elsewhere), the row reads as the wrapper name.
    expect(screen.getByText("Claude Code")).toBeInTheDocument();

    act(() => recordOptimisticTitle("conv_opt", "debug the login redirect"));

    const label = screen.getByText("debug the login redirect");
    expect(label).toHaveClass("italic", "text-muted-foreground");
    expect(screen.queryByText("Claude Code")).not.toBeInTheDocument();
  });

  it("renders a provisional (temp:) row as a bare navigable link with no mutating actions", () => {
    // A client-only temp row has no server session, so its per-row mutations
    // (kebab: rename/delete/archive/move/share) must be suppressed — invoking
    // them would POST to /v1/sessions/temp:* (Polly B-3).
    mockConversations([
      conv("temp:0a1b2c3d", "Claude Code", { title: "new chat", provisional: true }),
    ]);
    renderSidebar();

    // Navigable: the row is still a link into the (soon-to-exist) conversation.
    const link = screen.getByRole("link", { name: /new chat/ });
    expect(link).toHaveAttribute("href", expect.stringContaining("/c/temp:0a1b2c3d"));
    // But no action affordances until it's rekeyed to the real id.
    expect(screen.queryByRole("button", { name: "Conversation actions" })).not.toBeInTheDocument();
  });

  it("uses the interface text token for session-list errors", () => {
    conversationsRef.current = [];
    useConvMock.mockReturnValue({
      isLoading: false,
      isError: true,
      error: new Error("boom"),
    } as unknown as ReturnType<typeof useConversations>);
    renderSidebar();

    const error = screen.getByText("Failed to load: boom");
    expect(error).toHaveClass("text-ui");
    expect(error).not.toHaveClass("text-sm");
  });

  it.each([
    ["shared", "loading"],
    ["shared", "error"],
    ["all", "loading"],
    ["all", "error"],
    ["archived", "loading"],
    ["archived", "error"],
  ] as const)("keeps pins and expanded projects mounted during %s %s", (view, state) => {
    projectsMock.push("Work");
    mockConversations([
      conv("pinned-session", "Claude Code"),
      conv("filed-session", "Claude Code", { labels: { omni_project: "Work" } }),
    ]);
    seedPins(["pinned-session"]);
    const original = useConvMock.getMockImplementation()!;
    const retry = vi.fn();
    const mineRetry = vi.fn();
    useConvMock.mockImplementation((...args) => {
      const query = original(...args);
      if (args[4] !== (view === "archived" ? "archived" : "shared")) {
        return { ...query, refetch: mineRetry };
      }
      return {
        ...query,
        data: undefined,
        isLoading: state === "loading",
        isError: state === "error",
        error: state === "error" ? new Error("unavailable") : null,
        refetch: retry,
      } as ReturnType<typeof useConversations>;
    });
    renderSidebar();
    fireEvent.click(screen.getByRole("button", { name: "Work" }));
    const pinned = screen.getByText("pinned-session");
    const filed = screen.getByText("filed-session");
    const filter = screen.getByTestId("session-filter");

    selectSessionFilter(view);

    expect(screen.getByText("pinned-session")).toBe(pinned);
    expect(screen.getByText("filed-session")).toBe(filed);
    expect(screen.getByTestId("session-filter")).toBe(filter);
    const sessions = screen.getByRole("button", { name: "Sessions" }).closest("section")!;
    expect(within(sessions).getByRole("status")).toHaveTextContent(
      state === "loading" ? "Loading…" : /could not be loaded|Failed to load/,
    );
    if (state === "error") {
      fireEvent.click(within(sessions).getByRole("button", { name: "Retry" }));
      expect(retry).toHaveBeenCalledOnce();
    }
    selectSessionFilter("mine");
    expect(screen.getByText("pinned-session")).toBe(pinned);
    expect(screen.getByText("filed-session")).toBe(filed);
  });

  it("reveals a thin, theme-aware scrollbar only while scrolling", () => {
    vi.useFakeTimers();
    try {
      mockConversations(THREE_TYPE_CONVERSATIONS);
      renderSidebar();

      const scroller = screen.getByLabelText("Conversations").querySelector("nav")!;
      expect(scroller).toHaveClass("overflow-y-auto", "px-2", "[scrollbar-width:thin]");
      expect(scroller.className).toContain("[&::-webkit-scrollbar]:w-2");
      expect(scroller).not.toHaveClass("[scrollbar-width:none]");
      expect(scroller.className).not.toContain("[&::-webkit-scrollbar]:hidden");

      expect(scroller).toHaveClass("[scrollbar-color:transparent_transparent]");
      expect(scroller.className).toContain("[&::-webkit-scrollbar-thumb]:bg-transparent");
      expect(scroller).not.toHaveClass("[scrollbar-color:var(--muted-foreground)_transparent]");
      expect(scroller.className).not.toContain("[&::-webkit-scrollbar-thumb]:bg-muted-foreground");

      // Scrolling reveals the thumb.
      act(() => {
        fireEvent.scroll(scroller, { target: { scrollTop: 40 } });
      });
      expect(scroller).toHaveClass("[scrollbar-color:var(--muted-foreground)_transparent]");
      expect(scroller.className).toContain("[&::-webkit-scrollbar-thumb]:bg-muted-foreground");

      // It hides again once scrolling settles.
      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(scroller).toHaveClass("[scrollbar-color:transparent_transparent]");
      expect(scroller.className).toContain("[&::-webkit-scrollbar-thumb]:bg-transparent");
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows a draft icon only beside sessions with unfinished composer content", () => {
    mockConversations([
      conv("conv_draft", "Codex", { title: "Draft session" }),
      conv("conv_empty", "Codex", { title: "Empty session" }),
    ]);
    renderSidebar();

    const draftRow = screen.getByText("Draft session").closest("li")!;
    const emptyRow = screen.getByText("Empty session").closest("li")!;
    expect(within(draftRow).queryByTestId("conversation-draft-indicator")).toBeNull();

    act(() => setSessionDraft("conv_draft", { text: "unfinished message", files: [] }));

    const indicator = within(draftRow).getByTestId("conversation-draft-indicator");
    expect(indicator).toHaveAccessibleName("Draft");
    expect(indicator.parentElement).toHaveClass("absolute", "right-1");
    expect(within(emptyRow).queryByTestId("conversation-draft-indicator")).toBeNull();
  });

  it("uses balanced title padding until row actions are revealed", () => {
    mockConversations([conv("conv_balanced", "Codex", { title: "Balanced row title" })]);
    renderSidebar();

    const row = screen.getByText("Balanced row title").closest("a")!;
    // Mobile drops the pin + kebab, so at rest it reserves the same slim `pr-2`
    // as desktop; only desktop hover widens it for the revealed controls.
    expect(row).toHaveClass("pr-2");
    expect(row.className).not.toMatch(/(?:^|\s)pr-28(?:\s|$)/);
    expect(row.className).toContain("md:group-hover:pr-20");
    // Keyed on `:focus-visible`, matching when the trailing controls appear and
    // the state marker fades. `focus-within` would also fire for a plain click,
    // narrowing the reserve on the selected row while the marker stayed put.
    expect(row.className).toContain("md:group-has-[:focus-visible]:pr-20");
    expect(row.className).not.toContain("md:group-focus-within:pr-20");
    expect(row.className).not.toMatch(/(?:^|\s)md:pr-20(?:\s|$)/);
  });

  it("narrows the awaiting row's reserve on the same trigger that fades its tag", () => {
    // The "Needs response" tag is absolutely positioned, so the row's right
    // padding is the only thing keeping the title clear of it. If the padding
    // narrows on a trigger the tag's fade doesn't share, the title slides under
    // a still-visible tag — which is what a plain click did via `focus-within`.
    mockConversations([
      conv("conv_awaiting", "Claude Code", {
        title: "Awaiting row title",
        pending_elicitations_count: 1,
      }),
    ]);
    renderSidebar();

    const row = screen.getByText("Awaiting row title").closest("a")!;
    const tag = screen.getByTestId("session-state-badge");
    expect(tag).toHaveAttribute("data-state", "awaiting");

    // Every state that narrows the reserve must also fade the tag, and vice
    // versa, so the two can never disagree about whether the space is free.
    for (const trigger of ["md:group-hover:", "md:group-has-[:focus-visible]:"]) {
      expect(row.className).toContain(`${trigger}pr-20`);
      expect(tag.parentElement!.className).toContain(`${trigger}opacity-0`);
    }
    // `focus-within` fires for a plain mouse click, which the tag's fade does
    // not react to — the mismatch that put the title under the selected row's
    // tag.
    expect(row.className).not.toContain("focus-within");
  });

  it("centers a row's dot marker in a size-6 slot at the right-1 edge", () => {
    // The row dot and the collapsed-project dot share this geometry so they
    // line up vertically; keep the row side pinned here.
    mockConversations([
      conv("conv_running", "Claude Code", { title: "Running row", status: "running" }),
    ]);
    renderSidebar();

    const slot = screen.getByTestId("session-state-badge").parentElement!;
    expect(slot).toHaveClass("right-1", "w-6", "justify-center");
  });

  it("does not constrain a row's awaiting pill to the dot slot", () => {
    // The "Needs response" pill is wider than the dot markers; the size-6 box
    // would clip its label, so the pill keeps its natural width.
    mockConversations([
      conv("conv_awaiting", "Claude Code", {
        title: "Awaiting row",
        pending_elicitations_count: 1,
      }),
    ]);
    renderSidebar();

    const slot = screen.getByTestId("session-state-badge").parentElement!;
    expect(slot).toHaveClass("right-1");
    expect(slot).not.toHaveClass("w-6");
  });

  it("offers the four display filters and defaults to My sessions", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();

    fireEvent.pointerDown(screen.getByTestId("session-filter"), {
      button: 0,
      ctrlKey: false,
      pointerType: "mouse",
    });
    expect(screen.getByText("Display")).toBeInTheDocument();
    for (const value of ["all", "mine", "shared", "archived"]) {
      expect(screen.getByTestId(`session-filter-${value}`)).toBeInTheDocument();
    }
    // Radio semantics: exactly one option is checked, and it's "My sessions".
    expect(screen.getByTestId("session-filter-mine")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("session-filter-all")).toHaveAttribute("aria-checked", "false");
  });

  it("keeps the picked filter across a remount", () => {
    mockConversations([
      conv("conv_mine", "Claude Code"),
      conv("conv_shared", "Claude Code", { owner: "other@example.com" }),
    ]);
    renderSidebar();

    // Pick a non-default slice (default is "mine") so the remount below proves
    // the pick was persisted, not just that we landed back on the default.
    selectSessionFilter("shared");
    expect(screen.queryByText("conv_mine")).toBeNull();

    // Fresh mount re-reads localStorage: still scoped to shared sessions. If
    // this fails, the pick lived only in memory and a reload silently snapped
    // the list back to the default.
    cleanup();
    renderSidebar();
    expect(screen.getByText("conv_shared")).toBeInTheDocument();
    expect(screen.queryByText("conv_mine")).toBeNull();
    fireEvent.pointerDown(screen.getByTestId("session-filter"), {
      button: 0,
      ctrlKey: false,
      pointerType: "mouse",
    });
    expect(screen.getByTestId("session-filter-shared")).toHaveAttribute("aria-checked", "true");
  });

  it("withholds the conversation list until identity is ready", () => {
    mockConversations([conv("conv_mine", "Claude Code")]);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (identityReady: boolean) => (
      <QueryClientProvider client={qc}>
        <SidebarDataProvider identityReady={identityReady}>
          <ExtensionCatalogProvider extensions={[]}>
            <TooltipProvider>
              <MemoryRouter>
                <Sidebar open onClose={vi.fn()} />
              </MemoryRouter>
            </TooltipProvider>
          </ExtensionCatalogProvider>
        </SidebarDataProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(tree(false));

    expect(screen.queryByTestId("sidebar-conversation-list")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Loading sessions");

    rerender(tree(true));
    expect(screen.getByTestId("sidebar-conversation-list")).toBeInTheDocument();
    expect(screen.getByText("conv_mine")).toBeInTheDocument();
  });

  it("leaves Shared when runtime policy makes it unavailable", () => {
    localStorage.setItem("omnigent:session-filter", "shared");
    mockConversations([
      conv("conv_mine", "Claude Code"),
      conv("conv_shared", "Claude Code", { owner: "other@example.com" }),
    ]);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (config: SidebarConfig) => (
      <QueryClientProvider client={qc}>
        <SidebarDataProvider config={config}>
          <ExtensionCatalogProvider extensions={[]}>
            <TooltipProvider>
              <MemoryRouter>
                <Sidebar open onClose={vi.fn()} />
              </MemoryRouter>
            </TooltipProvider>
          </ExtensionCatalogProvider>
        </SidebarDataProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(tree(sidebarConfig));
    expect(screen.getByText("conv_shared")).toBeInTheDocument();

    rerender(tree({ ...sidebarConfig, sharedAvailable: false }));

    expect(screen.getByText("conv_mine")).toBeInTheDocument();
    expect(screen.queryByText("conv_shared")).toBeNull();
    expect(localStorage.getItem("omnigent:session-filter")).toBe("mine");
    fireEvent.pointerDown(screen.getByTestId("session-filter"), {
      button: 0,
      ctrlKey: false,
      pointerType: "mouse",
    });
    expect(screen.getByTestId("session-filter-mine")).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByTestId("session-filter-shared")).toBeNull();
  });

  it("drops a persisted Shared filter on a single-user server", () => {
    // "Shared sessions" isn't in the menu on a loopback-only server, so honoring
    // a value stored against a multi-user one would scope the list to a slice
    // the viewer has no option to leave.
    localStorage.setItem("omnigent:session-filter", "shared");
    isServerLocalMock.mockReturnValue(true);
    mockConversations([conv("conv_mine", "Claude Code")]);
    renderSidebar();

    expect(screen.getByText("conv_mine")).toBeInTheDocument();
    fireEvent.pointerDown(screen.getByTestId("session-filter"), {
      button: 0,
      ctrlKey: false,
      pointerType: "mouse",
    });
    expect(screen.getByTestId("session-filter-mine")).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByTestId("session-filter-shared")).toBeNull();
  });

  it("shows archived sessions only under the Archived filter", () => {
    mockConversations([
      conv("conv_live", "Claude Code"),
      conv("conv_done", "Claude Code", { archived: true }),
    ]);
    renderSidebar();

    // Every other filter hides archived sessions.
    expect(screen.getByText("conv_live")).toBeInTheDocument();
    expect(screen.queryByText("conv_done")).toBeNull();

    // The Archived filter shows them, and only them.
    selectSessionFilter("archived");
    expect(screen.getByText("conv_done")).toBeInTheDocument();
    expect(screen.queryByText("conv_live")).toBeNull();
  });

  it("re-scopes the list across every filter, in both directions", () => {
    // One fixture spanning all three states the filters slice on, cycled through
    // each option (and back) so a filter that leaks rows — or strands them after
    // a previous selection — fails here rather than only in one direction.
    mockConversations([
      conv("conv_owned", "Claude Code"),
      conv("conv_from_other", "Claude Code", { owner: "other@example.com" }),
      conv("conv_archived", "Claude Code", { archived: true }),
    ]);
    renderSidebar();

    const visible = () => ({
      owned: screen.queryByText("conv_owned") !== null,
      shared: screen.queryByText("conv_from_other") !== null,
      archived: screen.queryByText("conv_archived") !== null,
    });

    // Default: the viewer's own sessions only ("My sessions").
    expect(visible()).toEqual({ owned: true, shared: false, archived: false });

    selectSessionFilter("all");
    expect(visible()).toEqual({ owned: true, shared: true, archived: false });

    selectSessionFilter("shared");
    expect(visible()).toEqual({ owned: false, shared: true, archived: false });

    selectSessionFilter("archived");
    expect(visible()).toEqual({ owned: false, shared: false, archived: true });

    // Back to My sessions: leaving Archived restores the viewer's own rows.
    selectSessionFilter("mine");
    expect(visible()).toEqual({ owned: true, shared: false, archived: false });

    // And Archived is still reachable a second time (state isn't one-shot).
    selectSessionFilter("archived");
    expect(visible()).toEqual({ owned: false, shared: false, archived: true });
  });

  it("keeps the filter menu reachable when the chosen filter matches nothing", () => {
    // The filter lives on the Sessions header. Hiding that header on an empty
    // slice would strand the viewer on, say, Archived with no way back.
    mockConversations([conv("conv_live", "Claude Code")]);
    renderSidebar();

    selectSessionFilter("archived");
    expect(screen.queryByText("conv_live")).toBeNull();
    expect(screen.getAllByText("No sessions")[0]).toBeInTheDocument();

    // Still there, and still able to switch back.
    expect(screen.getByTestId("session-filter")).toBeInTheDocument();
    selectSessionFilter("all");
    expect(screen.getByText("conv_live")).toBeInTheDocument();
  });

  it("requests mine and shared scopes without an all-sessions scan", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();
    const calls = useConvMock.mock.calls;
    expect(calls.some((call) => call[1] === true)).toBe(false);
    expect(calls.find((call) => call[4] === "mine")?.[2]).toMatchObject({
      refreshIntervalMs: 60_000,
    });
    expect(calls.find((call) => call[4] === "shared")?.[2]).toMatchObject({
      refreshIntervalMs: 180_000,
    });
  });

  it("opens the command palette when the Search button is clicked", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    const onOpenSearch = vi.fn();
    renderSidebar(true, "/", onOpenSearch);

    // Session search moved into the command palette: the sidebar box is now a
    // button that opens it rather than an inline filter input.
    fireEvent.click(screen.getByTestId("sidebar-search-button"));
    expect(onOpenSearch).toHaveBeenCalledTimes(1);
  });

  it("swaps the card content to the settings section nav on /settings", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar(true, "/settings");

    // The same card now shows the settings nav (Back to app + sections),
    // not the conversation search/list.
    expect(screen.queryByTestId("sidebar-search-button")).toBeNull();
    expect(screen.getByRole("link", { name: "Back" })).toHaveAttribute("href", "/");
    expect(screen.getByTestId("settings-nav-appearance")).toHaveAttribute(
      "href",
      "/settings/appearance",
    );
    expect(screen.getByTestId("settings-nav-archived")).toHaveAttribute(
      "href",
      "/settings/archived",
    );
  });

  it("renders Search, Settings, and Collapse as compact header actions", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();

    const headerActions = screen.getByTestId("sidebar-header-actions");
    const search = within(headerActions).getByTestId("sidebar-search-button");
    const settings = screen.getByTestId("settings-button");

    expect(search).toHaveAttribute("aria-label", "Search");
    expect(search).toHaveAttribute("data-size", "icon-xs");
    expect(search).toHaveClass("size-6", "rounded-[var(--radius-md)]");
    expect(search).not.toHaveClass("rounded-sm");
    expect(search.querySelector("svg")).toHaveClass("size-4");
    expect(search.querySelector("svg")).not.toHaveClass("ui-icon");
    expect(settings).toHaveAttribute("aria-label", "Settings");
    expect(settings).toHaveAttribute("data-size", "icon-xs");
    expect(settings).toHaveClass("size-6", "rounded-[8px]");
    expect(settings.querySelector("svg")).toHaveClass("size-4");
    expect(settings.querySelector("svg")).not.toHaveClass("ui-icon");
    const collapse = within(headerActions).getByRole("button", { name: "Close sidebar" });
    expect(collapse).toHaveAttribute("data-size", "icon-xs");
    expect(collapse).toHaveClass("size-6", "rounded-[var(--radius-md)]");
    expect(within(headerActions).queryByTestId("inbox-button")).toBeNull();
  });

  it("omits the desktop server picker outside the Electron shell", async () => {
    // The picker is Electron-only: it self-hides when the native bridge
    // reports no connected server (a plain browser tab, as in tests), leaving
    // the sidebar ending with the session list exactly as before.
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();

    // Anchor on something that DOES render, so a silently-empty sidebar can't
    // make this assertion pass for the wrong reason.
    expect(await screen.findByTestId("settings-button")).toBeInTheDocument();
    expect(screen.queryByTestId("sidebar-server-picker")).toBeNull();
    expect(screen.queryByTestId("sidebar-server-picker-row")).toBeNull();
  });

  it("renders Inbox as its own primary navigation row", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();

    const primaryNav = screen.getByTestId("sidebar-primary-nav");
    const inbox = within(primaryNav).getByTestId("inbox-button");
    const newChat = within(primaryNav).getByTestId("new-chat-button");

    expect(inbox).toHaveAttribute("href", "/inbox");
    expect(inbox).toHaveClass(
      "sidebar-row",
      "h-auto",
      "min-h-0",
      "w-full",
      "justify-start",
      "gap-2",
      "px-2",
      "py-1.5",
      "md:py-1",
    );
    expect(inbox).toHaveClass("hover:bg-muted", "hover:text-foreground", "dark:hover:bg-muted/50");
    expect(inbox.className).not.toContain("sidebar-hover");
    expect(inbox).not.toHaveClass("h-8");
    expect(newChat.querySelector("svg")).toHaveClass("text-[var(--sidebar-active-foreground)]");
    expect(inbox.querySelector("svg")).toHaveClass("text-muted-foreground");
    expect(within(inbox).getByText("Inbox")).toBeInTheDocument();
    expect(within(primaryNav).queryByTestId("toggle-selection-mode")).toBeNull();
  });

  it("paints the Inbox count with the shared active treatment while Inbox is inactive", () => {
    // Off the /inbox route the badge is the only carrier of the count's
    // treatment, so it wears the same --sidebar-active wash and foreground as
    // a selected session row.
    mockConversations([conv("conv_awaiting", "Claude Code", { pending_elicitations_count: 2 })]);
    renderSidebar();

    const inbox = screen.getByTestId("inbox-button");
    expect(inbox).not.toHaveClass("bg-[var(--sidebar-active)]");
    const badge = within(inbox).getByLabelText("2 inbox items waiting");
    expect(badge).toHaveClass(
      "bg-[var(--sidebar-active)]",
      "text-[var(--sidebar-active-foreground)]",
    );
    expect(badge.className).not.toContain("brand-accent");
  });

  it("lets the active Inbox row show through the count badge", () => {
    // On /inbox the row itself paints the translucent --sidebar-active wash.
    // The badge keeps the shared active foreground but stays transparent so
    // the wash is not double-composited into a darker fill.
    mockConversations([conv("conv_awaiting", "Claude Code", { pending_elicitations_count: 2 })]);
    renderSidebar(true, "/inbox");

    const inbox = screen.getByTestId("inbox-button");
    expect(inbox).toHaveClass(
      "bg-[var(--sidebar-active)]",
      "text-[var(--sidebar-active-foreground)]",
    );
    const badge = within(inbox).getByLabelText("2 inbox items waiting");
    expect(badge).toHaveClass("bg-transparent", "text-[var(--sidebar-active-foreground)]");
    expect(badge).not.toHaveClass("bg-[var(--sidebar-active)]");
  });

  it("hides Usage navigation while the release feature is off", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();

    expect(screen.queryByTestId("usage-nav")).toBeNull();
  });

  it("shows and highlights Usage navigation when the release feature is on", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar(true, "/usage", undefined, {
      ...FALLBACK_SERVER_INFO,
      features: { usage_page: true },
    });

    const usage = screen.getByTestId("usage-nav");
    expect(usage).toHaveAttribute("href", "/usage");
    expect(usage).toHaveClass("bg-[var(--sidebar-active)]");
  });

  it("hides Canvas navigation while the release feature is off", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar(true, "/canvas");

    expect(screen.queryByTestId("canvas-nav")).toBeNull();
  });

  it("renders and highlights the Canvas nav row without lighting New session", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar(true, "/canvas", undefined, {
      ...FALLBACK_SERVER_INFO,
      features: { canvas: true },
    });

    const canvas = screen.getByTestId("canvas-nav");
    expect(canvas).toHaveAttribute("href", "/canvas");
    expect(canvas).toHaveAttribute("aria-current", "page");
    expect(canvas).toHaveClass("bg-[var(--sidebar-active)]");
    expect(screen.getByTestId("new-chat-button")).not.toHaveClass("bg-[var(--sidebar-active)]");
  });

  it.each(["/canvas", "/canvas/c/conv_one"])(
    "keeps the Canvas navigation destination on the selected project at %s",
    (pathname) => {
      mockConversations(THREE_TYPE_CONVERSATIONS);
      renderSidebar(true, `${pathname}?canvas=project`, undefined, {
        ...FALLBACK_SERVER_INFO,
        features: { canvas: true },
      });

      expect(screen.getByTestId("canvas-nav")).toHaveAttribute("href", "/canvas?canvas=project");
      expect(screen.getByTestId("canvas-nav")).toHaveAttribute("aria-current", "page");
    },
  );

  it("keeps filtering visible while session selection remains hover-revealed", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();

    const sessionsSection = screen.getByText("Sessions").closest("section");
    expect(sessionsSection).not.toBeNull();

    const newSession = within(sessionsSection!).getByRole("link", {
      name: "New session",
    });
    expect(newSession).toHaveAttribute("data-testid", "sessions-new-session");
    expect(newSession).toHaveAttribute("href", "/");
    expect(newSession).toHaveAttribute("data-size", "icon-xs");
    expect(newSession).toHaveClass("text-muted-foreground", "hover:text-foreground");
    expect(newSession).not.toHaveTextContent("New session");

    const selectSessions = within(sessionsSection!).getByRole("button", {
      name: "Select sessions",
    });
    expect(selectSessions).toHaveAttribute("data-testid", "toggle-selection-mode");
    expect(selectSessions).toHaveAttribute("data-size", "icon-xs");
    expect(selectSessions).toHaveClass("text-muted-foreground", "hover:text-foreground");
    expect(selectSessions).not.toHaveTextContent("Select sessions");
    expect(
      newSession.compareDocumentPosition(selectSessions) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(selectSessions.parentElement).toHaveClass("flex", "gap-0.5");
    expect(selectSessions.parentElement?.parentElement).toHaveClass(
      "[@media((hover:hover)_and_(pointer:fine))]:md:opacity-0",
      "[@media((hover:hover)_and_(pointer:fine))]:md:group-hover/header:opacity-100",
      "[@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-header-controls]:focus-within]/header:opacity-100",
      "[@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-testid=session-filter][aria-expanded=true]]/header:opacity-100",
    );

    const filterSessions = within(sessionsSection!).getByRole("button", {
      name: "Filter sessions",
    });
    // The filter never fades; its persistent-action wrapper re-enables hit-testing
    // inside the pointer-events-gated outer box (see the overlay hit-test spec below).
    const filterTooltipTrigger = filterSessions.parentElement!;
    const filterPersistentAction = filterTooltipTrigger.parentElement!;
    expect(filterTooltipTrigger).not.toHaveClass("md:opacity-0");
    expect(filterPersistentAction).toHaveClass("pointer-events-auto", "flex");
    expect(filterPersistentAction.parentElement).toHaveClass("absolute", "right-1", "flex");

    fireEvent.click(selectSessions);
    expect(screen.getByRole("button", { name: "Exit selection mode" })).toBeInTheDocument();
    expect(within(sessionsSection!).queryByRole("button", { name: "Select sessions" })).toBeNull();
    expect(within(sessionsSection!).queryByRole("link", { name: "New session" })).toBeNull();
  });

  it("renders the 'Automations' nav row directly under 'New session' and routes to /tasks", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();

    const scheduled = screen.getByTestId("scheduled-tasks-nav");
    // Full-width nav ROW (a link), labeled "Automations", pointing at /tasks —
    // not the old top-right icon button.
    expect(scheduled).toHaveAttribute("href", "/tasks");
    expect(scheduled).toHaveTextContent("Automations");
    // The removed icon-button version must be gone.
    expect(screen.queryByTestId("scheduled-tasks-button")).toBeNull();

    // It sits in the primary nav group right after "New session" and before
    // the "Inbox" row. Compare document order. (The search box now renders
    // above this nav group upstream, so we anchor to New session + Inbox — the
    // two items actually adjacent to Scheduled — rather than the search box.)
    const newSession = screen.getByTestId("new-chat-button");
    const inbox = screen.getByTestId("inbox-button");
    expect(newSession.compareDocumentPosition(scheduled) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(scheduled.compareDocumentPosition(inbox) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("reveals the new-session shortcut within the row on hover or keyboard focus", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();

    const newSession = screen.getByTestId("new-chat-button");
    const shortcut = newSession.querySelector<HTMLElement>('[data-slot="shortcut-keys"]');

    expect(newSession).toHaveAttribute("aria-keyshortcuts", `${ARIA_MOD_KEY}+Alt+N`);
    expect(
      Array.from(newSession.querySelectorAll('[data-slot="kbd"]'), (key) => key.textContent),
    ).toEqual([MOD_KEY, ALT_KEY, "N"]);
    expect(shortcut).toHaveClass(
      "absolute",
      "top-1/2",
      "right-2",
      "-translate-y-1/2",
      "opacity-0",
      "group-focus-visible/new-session:opacity-100",
      "[@media((hover:hover)_and_(pointer:fine))]:group-hover/new-session:opacity-100",
    );
  });

  it("marks the 'Automations' nav row active when on /tasks", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar(true, "/tasks");

    // Active/selected state uses the SAME shared active-highlight as the sibling
    // nav rows (New session / Inbox) — the `--sidebar-active` pill, not an
    // ad-hoc bg-muted.
    expect(screen.getByTestId("scheduled-tasks-nav")).toHaveClass(
      "bg-[var(--sidebar-active)]",
      "dark:hover:bg-[var(--sidebar-active)]",
      "dark:hover:text-[var(--sidebar-active-foreground)]",
    );
  });

  it("renders and activates an extension nav row without activating core rows", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar(true, "/extensions/acme.review/inbox", undefined, undefined, [TEST_EXTENSION]);

    const extension = screen.getByTestId("extension-nav-acme.review.primary-nav");
    expect(extension).toHaveAttribute("href", "/extensions/acme.review/inbox");
    expect(extension).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("new-chat-button")).not.toHaveAttribute("aria-current");
    expect(screen.getByTestId("inbox-button")).not.toHaveAttribute("aria-current");
  });

  it("closes the mobile sidebar when an extension nav row is selected", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    const onClose = vi.fn();
    renderSidebar(true, "/", undefined, undefined, [TEST_EXTENSION], onClose);

    fireEvent.click(screen.getByTestId("extension-nav-acme.review.primary-nav"));

    expect(onClose).toHaveBeenCalledOnce();
  });

  it("leaves the primary nav unchanged for an empty extension catalog", () => {
    mockConversations(THREE_TYPE_CONVERSATIONS);
    renderSidebar();

    expect(screen.queryByTestId(/^extension-nav-/)).toBeNull();
    expect(screen.getByTestId("new-chat-button")).toBeInTheDocument();
    expect(screen.getByTestId("scheduled-tasks-nav")).toBeInTheDocument();
    expect(screen.getByTestId("inbox-button")).toBeInTheDocument();
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
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/"]}>
              <Sidebar open onClose={onClose} />
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
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
    // Active sessions still render in the Sessions list.
    const recentSection = screen.getByText("Sessions").closest("section")!;
    expect(within(recentSection).getByText("conv_active")).toBeInTheDocument();
    // The header Settings link points at the settings page.
    expect(screen.getByTestId("settings-button")).toHaveAttribute("href", "/settings");
  });

  it("renders sessions in one flat list with no connection grouping", () => {
    // Liveness grouping is gone: sessions are no longer split into
    // Connected / Disconnected sections. They all land in one flat list under
    // the baseline "Sessions" header. The per-row lifecycle badge still shows
    // for a running session (the badge no longer reflects runner connection
    // state).
    const online = conv("conv_online", "Codex", { status: "running" });
    const offline = conv("conv_offline", "Claude Code", { status: "running" });
    mockConversations([online, offline]);

    renderSidebar();

    // No connection-grouping headings; the flat list keeps its "Sessions" header.
    expect(screen.queryByRole("heading", { name: "Connected" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Disconnected" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Sessions" })).toBeInTheDocument();

    // Both rows render in the flat list, and the online running session shows
    // its lifecycle badge (in the row's time-marker slot, outside the link).
    expect(screen.getByRole("link", { name: /conv_offline/ })).toBeInTheDocument();
    const onlineRow = screen.getByRole("link", { name: /conv_online/ }).closest("li")!;
    expect(within(onlineRow).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "running",
    );
  });

  it("shows session-state badges without rendering session timestamps", () => {
    // Fresh updated_at would render "now" if timestamps leaked back into
    // session rows.
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

    // Working rows keep their lifecycle badge without a timestamp.
    const workingRow = screen.getByRole("link", { name: /conv_working/ }).closest("li")!;
    expect(within(workingRow).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "running",
    );
    expect(within(workingRow).queryByText("now")).toBeNull();

    // Awaiting rows keep the "Needs response" badge without a timestamp.
    const awaitingRow = screen.getByRole("link", { name: /conv_awaiting/ }).closest("li")!;
    expect(within(awaitingRow).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "awaiting",
    );
    expect(within(awaitingRow).queryByText("now")).toBeNull();

    // Idle rows render neither a lifecycle badge nor a timestamp.
    const idleRow = screen.getByRole("link", { name: /conv_idle/ }).closest("li")!;
    expect(within(idleRow).queryByTestId("session-state-badge")).toBeNull();
    expect(within(idleRow).queryByText("now")).toBeNull();
  });

  it("shows a starting spinner on the bound session while a send is waking it", () => {
    // The launch/relaunch window: the user sent a message (local status
    // "streaming") but the server hasn't confirmed `running` — a cold boot or
    // a send waking a disconnected runner. The row's server status is stale
    // ("failed" after a runner disconnect), yet the sidebar must show the
    // session is coming up, matching the chat's "Starting up…" indicator.
    mockConversations([
      conv("conv_waking", "Claude Code", { status: "failed" }),
      conv("conv_other", "Claude Code"),
    ]);
    useChatStore.setState({ conversationId: "conv_waking", status: "streaming" });

    renderSidebar();

    const wakingRow = screen.getByRole("link", { name: /conv_waking/ }).closest("li")!;
    expect(within(wakingRow).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "starting",
    );
    // Only the bound session reads the store signal — other rows stay bare.
    const otherRow = screen.getByRole("link", { name: /conv_other/ }).closest("li")!;
    expect(within(otherRow).queryByTestId("session-state-badge")).toBeNull();
  });

  it("shows the full session title in a styled tooltip on hover", async () => {
    const title = "A long session title that is truncated in the compact sidebar row";
    const now = new Date("2026-07-23T00:00:00Z").getTime();
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(now);
    mockConversations([
      conv("conv_tooltip", "Codex", {
        title,
        updated_at: (now - 2 * 24 * 60 * 60 * 1000) / 1000,
      }),
    ]);
    renderSidebar();

    const row = screen.getByRole("link", { name: title });
    expect(row).not.toHaveAttribute("title");

    fireEvent.pointerMove(row, { pointerType: "mouse" });
    await waitFor(() => {
      const tooltip = screen.getByTestId("session-tooltip-content");
      expect(tooltip).toHaveTextContent(title);
      // The tooltip mirrors the pinned-project flyout's compact Tooltip look
      // (bg-popover surface), not the old wide card.
      expect(tooltip.className).toContain("bg-popover");
      expect(tooltip.className).not.toContain("bg-card-solid");
      // The title matches sidebar row names through the shared compact class,
      // which resolves to the same text-ui step as Appearance content.
      const tooltipTitle = tooltip.querySelector("p.sidebar-compact-text");
      expect(tooltipTitle).not.toBeNull();
      expect(tooltipTitle).toHaveTextContent(title);
      expect(tooltip).toHaveTextContent("2d");
      expect(within(tooltip).getAllByTestId("session-tooltip-location")[0]).toHaveTextContent(
        "Local machine",
      );
    });
    nowSpy.mockRestore();
  });

  it("keeps title-only and worktree session rows the same centered height", () => {
    mockConversations([
      conv("conv_plain", "Codex", { title: "Plain session" }),
      conv("conv_worktree", "Codex", {
        title: "Worktree session",
        git_branch: "fix/sidebar-row-height",
      }),
    ]);
    renderSidebar();

    const plainRow = screen.getByText("Plain session").closest("a")!;
    const worktreeRow = screen.getByText("Worktree session").closest("a")!;

    expect(plainRow).toHaveClass("sidebar-row", "h-auto", "min-h-0", "justify-center");
    expect(worktreeRow).toHaveClass("sidebar-row", "h-auto", "min-h-0", "justify-center");
    expect(within(worktreeRow).queryByText("fix/sidebar-row-height")).toBeNull();
  });

  it("reveals git branch metadata in the session tooltip on hover", async () => {
    const branch = "fix/sidebar-row-height";
    mockConversations([
      conv("conv_branch_tooltip", "Codex", {
        title: "Worktree session",
        git_branch: branch,
      }),
    ]);
    renderSidebar();

    const row = screen.getByText("Worktree session").closest("a")!;
    expect(within(row).queryByText(branch)).toBeNull();

    fireEvent.pointerMove(row, { pointerType: "mouse" });
    await waitFor(() => {
      expect(screen.getAllByTestId("session-tooltip-branch")[0]).toHaveTextContent(branch);
    });
  });

  describe("session tooltip details", () => {
    // Every non-spinner state draws one filled, token-coloured dot that falls
    // back to the system text colour in forced-colors mode.
    const HARD_CODED_COLOR =
      /#[0-9a-f]{3,8}|rgba?\(|oklch\(|hsla?\(|-(?:gray|zinc|slate|neutral|stone|red|pink|rose|blue|green|white|black)(?:-\d|\b)/i;
    function expectStatusDot(status: HTMLElement, token: string) {
      expect(status.querySelector("svg")).toBeNull();
      const dot = status.querySelector("span[aria-hidden] > span.rounded-full");
      expect(dot).toHaveClass("size-2", token, "forced-colors:bg-[CanvasText]");
      expect(dot?.className).not.toMatch(/\bborder\b/);
      expect(dot?.className).not.toMatch(HARD_CODED_COLOR);
    }

    async function hoverTooltip(session: Conversation) {
      mockConversations([session]);
      renderSidebar();
      fireEvent.pointerMove(screen.getByRole("link", { name: new RegExp(session.id) }), {
        pointerType: "mouse",
      });
      return screen.findByTestId("session-tooltip-content");
    }

    it("widens the card while keeping the viewport clamp", async () => {
      const tooltip = await hoverTooltip(conv("conv_width", "aria"));
      expect(tooltip).toHaveClass("w-72", "max-w-[calc(100vw-2rem)]");
    });

    it.each([false, true])(
      "loads native composer metadata only on open, once across rows (failed=%s)",
      async (failed) => {
        useHostsMock.mockReturnValue({
          data: [{ host_id: "host_model", name: "Laptop", status: "online" }],
        });
        let resolveCatalog!: (response: Response) => void;
        const catalog = new Promise<Response>((resolve) => {
          resolveCatalog = resolve;
        });
        const fetchSpy = vi
          .spyOn(identity, "authenticatedFetch")
          .mockImplementation((url) =>
            String(url).includes("/model-options")
              ? catalog
              : Promise.resolve(new Response(JSON.stringify({ data: [] }), { status: 200 })),
          );
        const row = (id: string) =>
          conv(id, "claude-native-ui", {
            host_id: "host_model",
            llm_model: "opus[1m]",
            reasoning_effort: "medium",
            labels: { "omnigent.wrapper": "claude-code-native-ui" },
          });
        mockConversations([row("native_one"), row("native_two")]);
        renderSidebar();
        const calls = () =>
          fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options"));
        expect(calls()).toHaveLength(0);
        fireEvent.focus(screen.getByRole("link", { name: "native_one" }));
        const tooltip = await screen.findByTestId("session-tooltip-content");
        const agentLine = within(tooltip).getAllByTestId("session-tooltip-agent")[0];
        expect(agentLine).toHaveTextContent(/^Claude Code Medium$/);
        expect(agentLine.querySelector("img")).toHaveAttribute("src", claudeCodeLogo);
        await waitFor(() => expect(calls()).toHaveLength(1));
        await act(async () =>
          resolveCatalog(
            new Response(
              JSON.stringify(
                failed
                  ? { detail: "offline" }
                  : {
                      models: [{ id: "opus[1m]", displayName: "Opus 5.5 (1M context)" }],
                    },
              ),
              { status: failed ? 503 : 200 },
            ),
          ),
        );
        await waitFor(() =>
          expect(agentLine).toHaveTextContent(
            failed ? /^Claude Code Medium$/ : /^Opus 5.5 1M Medium$/,
          ),
        );
        fireEvent.blur(screen.getByRole("link", { name: "native_one" }));
        await waitFor(() => expect(screen.queryByTestId("session-tooltip-content")).toBeNull());
        if (failed) {
          fetchSpy.mockImplementation(
            async () =>
              new Response(
                JSON.stringify({
                  models: [{ id: "opus[1m]", displayName: "Opus 5.5 (1M context)" }],
                }),
              ),
          );
        }
        vi.spyOn(Date, "now").mockReturnValue(Date.now() + 60_000);
        fireEvent.focus(screen.getByRole("link", { name: "native_two" }));
        const second = await screen.findByTestId("session-tooltip-content");
        await waitFor(() =>
          expect(within(second).getAllByTestId("session-tooltip-agent")[0]).toHaveTextContent(
            failed ? /^Claude Code Medium$/ : /^Opus 5.5 1M Medium$/,
          ),
        );
        expect(calls()).toHaveLength(1);
        fetchSpy.mockRestore();
        vi.mocked(Date.now).mockRestore();
      },
    );

    it.each<{
      field: keyof Conversation;
      initial: Partial<Conversation>;
      update: Partial<Conversation>;
      testId: string;
      before: string;
      after: string;
    }>([
      {
        field: "agent_id",
        initial: { agent_id: "ag_old" },
        update: { agent_id: "ag_new" },
        testId: "session-tooltip-agent",
        before: "Old agent · Claude SDK",
        after: "New agent · Claude SDK",
      },
      {
        field: "agent_name",
        initial: { agent_name: "old-agent" },
        update: { agent_name: "new-agent" },
        testId: "session-tooltip-agent",
        before: "old-agent",
        after: "new-agent",
      },
      {
        field: "workspace",
        initial: { workspace: "/old/workspace" },
        update: { workspace: "/new/workspace" },
        testId: "session-tooltip-cwd",
        before: "/old/workspace",
        after: "/new/workspace",
      },
      {
        field: "llm_model",
        initial: { agent_name: "claude-native-ui", llm_model: "opus[1m]" },
        update: { llm_model: "sonnet" },
        testId: "session-tooltip-agent",
        before: "Opus 5.5 1M Medium",
        after: "Sonnet Medium",
      },
      {
        field: "harness_override",
        initial: { agent_name: "claude-native-ui" },
        update: { harness_override: "cursor-native" },
        testId: "session-tooltip-agent",
        before: "Claude Code Medium",
        after: "Cursor",
      },
      {
        field: "reasoning_effort",
        initial: { agent_name: "claude-native-ui", reasoning_effort: "medium" },
        update: { reasoning_effort: "high" },
        testId: "session-tooltip-agent",
        before: "Claude Code Medium",
        after: "Claude Code High",
      },
      {
        field: "host_online",
        initial: { agent_name: "claude-native-ui", llm_model: "opus[1m]", host_online: true },
        update: { host_online: false },
        testId: "session-tooltip-agent",
        before: "Opus 5.5 1M Medium",
        after: "Claude Code Medium",
      },
    ])("refreshes an open tooltip after a metadata-only $field update", async (testCase) => {
      agentsRef.current = [
        {
          id: "ag_old",
          name: "old-known",
          display_name: "Old agent",
          harness: "claude-sdk",
          description: null,
          skills: [],
        },
        {
          id: "ag_new",
          name: "new-known",
          display_name: "New agent",
          harness: "claude-sdk",
          description: null,
          skills: [],
        },
      ];
      useHostsMock.mockReturnValue({
        data: [{ host_id: "host_model", name: "Laptop", status: "online" }],
      });
      vi.spyOn(identity, "authenticatedFetch").mockImplementation(
        async () =>
          new Response(
            JSON.stringify({
              models: [
                { id: "opus[1m]", displayName: "Opus 5.5 (1M context)" },
                { id: "sonnet", displayName: "Sonnet" },
              ],
            }),
          ),
      );
      const session = conv("metadata_row", "unknown-agent", {
        host_id: "host_model",
        reasoning_effort: "medium",
        labels:
          testCase.initial.agent_name === "claude-native-ui"
            ? { "omnigent.wrapper": "claude-code-native-ui" }
            : {},
        updated_at: 100,
        ...testCase.initial,
      });
      mockConversations([session]);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const tree = () => (
        <QueryClientProvider client={client}>
          <SidebarDataProvider>
            <TooltipProvider>
              <MemoryRouter>
                <Sidebar open onClose={vi.fn()} />
              </MemoryRouter>
            </TooltipProvider>
          </SidebarDataProvider>
        </QueryClientProvider>
      );
      const { rerender } = render(tree());
      fireEvent.focus(screen.getByRole("link", { name: "metadata_row" }));
      const tooltip = await screen.findByTestId("session-tooltip-content");
      await waitFor(() =>
        expect(within(tooltip).getAllByTestId(testCase.testId)[0]).toHaveTextContent(
          testCase.before,
        ),
      );

      mockConversations([{ ...session, ...testCase.update }]);
      rerender(tree());

      await waitFor(() =>
        expect(within(tooltip).getAllByTestId(testCase.testId)[0]).toHaveTextContent(
          testCase.after,
        ),
      );
    });

    it.each([false, true])(
      "uses the native fallback without probing an offline host or old server (offline=%s)",
      async (offline) => {
        useHostsMock.mockReturnValue({
          data: [{ host_id: "host_model", name: "Laptop", status: offline ? "offline" : "online" }],
        });
        const fetchSpy = vi.spyOn(identity, "authenticatedFetch");
        const tooltip = await hoverTooltip(
          conv("native_fallback", "claude-native-ui", {
            host_id: "host_model",
            llm_model: offline ? "opus" : undefined,
            reasoning_effort: "medium",
            labels: { "omnigent.wrapper": "claude-code-native-ui" },
          }),
        );
        expect(within(tooltip).getAllByTestId("session-tooltip-agent")[0]).toHaveTextContent(
          /^Claude Code Medium$/,
        );
        expect(
          fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
        ).toHaveLength(0);
        fetchSpy.mockRestore();
      },
    );

    it("resolves per-session harness overrides for two rows of the same agent", async () => {
      agentsRef.current = [
        {
          id: "ag_aria",
          name: "aria",
          display_name: "Aria",
          harness: "claude-sdk",
          description: null,
          skills: [],
        },
      ];
      const fetchSpy = vi.spyOn(identity, "authenticatedFetch");
      mockConversations([
        conv("sdk_default", "aria", { agent_id: "ag_aria" }),
        conv("sdk_override", "aria", { agent_id: "ag_aria", harness_override: "codex" }),
      ]);
      renderSidebar();
      async function checkRow(title: string, expected: string) {
        const row = screen.getByRole("link", { name: title });
        fireEvent.focus(row);
        const tooltip = await screen.findByTestId("session-tooltip-content");
        expect(within(tooltip).getAllByTestId("session-tooltip-agent")[0]).toHaveTextContent(
          expected,
        );
        fireEvent.blur(row);
        await waitFor(() => expect(screen.queryByTestId("session-tooltip-content")).toBeNull());
      }
      await checkRow("sdk_default", "Aria · Claude SDK");
      await checkRow("sdk_override", "Aria · Codex");
      expect(
        fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
      ).toHaveLength(0);
    });

    it("resolves same-named custom agents only by their bound id", async () => {
      agentsRef.current = [
        {
          id: "ag_deploy_old",
          name: "deploy-bot",
          display_name: "Deploy Bot",
          harness: "claude-sdk",
          description: null,
          skills: [],
        },
        {
          id: "ag_deploy_new",
          name: "deploy-bot",
          display_name: "Deploy Bot",
          harness: "codex",
          description: null,
          skills: [],
        },
      ];
      const fetchSpy = vi.spyOn(identity, "authenticatedFetch");
      mockConversations([
        conv("deploy_old", "deploy-bot", { agent_id: "ag_deploy_old" }),
        conv("deploy_new", "deploy-bot", { agent_id: "ag_deploy_new" }),
        conv("deploy_missing", "deploy-bot", {
          agent_id: "ag_deploy_missing",
          llm_model: "opus[1m]",
          reasoning_effort: "high",
        }),
      ]);
      renderSidebar();
      async function checkRow(title: string, expected: string) {
        const row = screen.getByRole("link", { name: title });
        fireEvent.focus(row);
        const tooltip = await screen.findByTestId("session-tooltip-content");
        const agentLine = within(tooltip).getAllByTestId("session-tooltip-agent")[0];
        expect(agentLine).toHaveTextContent(new RegExp(`^${expected}$`));
        if (title === "deploy_missing") {
          expect(agentLine.querySelector(".lucide-bot")).not.toBeNull();
          expect(agentLine.querySelector("img")).toBeNull();
        }
        fireEvent.blur(row);
        await waitFor(() => expect(screen.queryByTestId("session-tooltip-content")).toBeNull());
      }
      await checkRow("deploy_old", "Deploy Bot · Claude SDK");
      await checkRow("deploy_new", "Deploy Bot · Codex");
      await checkRow("deploy_missing", "deploy-bot");
      expect(
        fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
      ).toHaveLength(0);
    });

    it.each([
      ["codex-native", "Codex High", true],
      ["codex", "Bundle · Codex", false],
      [null, "bundle", false],
      [undefined, "Bundle · Claude SDK", false],
    ] as const)(
      "uses the server child harness %s without inheriting the bundle",
      async (harness, expected, catalog) => {
        agentsRef.current = [
          {
            id: "ag_bundle",
            name: "bundle",
            display_name: "Bundle",
            harness: "claude-sdk",
            description: null,
            skills: [],
          },
        ];
        useHostsMock.mockReturnValue({
          data: [{ host_id: "host_child", name: "Laptop", status: "online" }],
        });
        const fetchSpy = vi
          .spyOn(identity, "authenticatedFetch")
          .mockImplementation(async () => new Response(JSON.stringify({ models: [] })));
        const tooltip = await hoverTooltip(
          conv("child_row", "bundle", {
            agent_id: "ag_bundle",
            parent_session_id: "parent",
            host_id: "host_child",
            llm_model: "model",
            reasoning_effort: "high",
            ...(harness === undefined ? {} : { child_harness: harness }),
          }),
        );
        const line = within(tooltip).getAllByTestId("session-tooltip-agent")[0];
        expect(line).toHaveTextContent(new RegExp(`^${expected}$`));
        const descriptor = buildComposerSessionDescriptor(harness ?? "claude-sdk", {}, "parent");
        expect(composerModelChipLabel({ session: descriptor, effort: "high" }).effortLabel).toBe(
          harness === "codex-native" ? "High" : null,
        );
        if (harness === null) {
          expect(line.querySelector(".lucide-bot")).not.toBeNull();
          expect(line.querySelector("img")).toBeNull();
        }
        if (harness === "codex-native")
          expect(line.querySelector("[data-harness-icon='codex']")).not.toBeNull();
        await waitFor(() =>
          expect(
            fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
          ).toHaveLength(catalog ? 1 : 0),
        );
        if (catalog)
          expect(
            String(fetchSpy.mock.calls.find(([url]) => String(url).includes("/model-options"))![0]),
          ).toContain("/harnesses/codex-native/model-options");
      },
    );

    it("updates routing parity on a mode-only change without querying the catalog", async () => {
      const info = {
        ...FALLBACK_SERVER_INFO,
        smart_routing_enabled: true,
        smart_routing_sources: { external: true, oss: false },
      };
      useHostsMock.mockReturnValue({
        data: [{ host_id: "host_routing", name: "Laptop", status: "online" }],
      });
      const fetchSpy = vi
        .spyOn(identity, "authenticatedFetch")
        .mockImplementation(
          async () =>
            new Response(JSON.stringify({ models: [{ id: "opus", displayName: "Opus" }] })),
        );
      const session = conv("routing_row", "claude-native-ui", {
        host_id: "host_routing",
        llm_model: "opus",
        reasoning_effort: "high",
        cost_control_mode_override: "on",
        updated_at: 100,
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
      });
      mockConversations([session]);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const tree = () => (
        <QueryClientProvider client={client}>
          <SidebarDataProvider>
            <CapabilitiesProvider info={info}>
              <TooltipProvider>
                <MemoryRouter>
                  <Sidebar open onClose={vi.fn()} />
                </MemoryRouter>
              </TooltipProvider>
            </CapabilitiesProvider>
          </SidebarDataProvider>
        </QueryClientProvider>
      );
      const { rerender } = render(tree());
      fireEvent.focus(screen.getByRole("link", { name: "routing_row" }));
      const tooltip = await screen.findByTestId("session-tooltip-content");
      expect(within(tooltip).getAllByTestId("session-tooltip-agent")[0]).toHaveTextContent(
        /^Smart Routing$/,
      );
      expect(
        fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
      ).toHaveLength(0);
      mockConversations([{ ...session, cost_control_mode_override: "off" }]);
      rerender(tree());
      await waitFor(() =>
        expect(within(tooltip).getAllByTestId("session-tooltip-agent")[0]).toHaveTextContent(
          /^Opus High$/,
        ),
      );
      expect(
        fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
      ).toHaveLength(1);
    });

    it.each(["codex", undefined] as const)(
      "refreshes an open child tooltip after a %s harness-only metadata update",
      async (initialHarness) => {
        agentsRef.current = [
          {
            id: "ag_bundle",
            name: "bundle",
            display_name: "Bundle",
            harness: "claude-sdk",
            description: null,
            skills: [],
          },
        ];
        const session = conv("child_metadata", "bundle", {
          agent_id: "ag_bundle",
          parent_session_id: "parent",
          ...(initialHarness === undefined ? {} : { child_harness: initialHarness }),
          updated_at: 100,
        });
        mockConversations([session]);
        const client = new QueryClient();
        const tree = () => (
          <QueryClientProvider client={client}>
            <SidebarDataProvider>
              <TooltipProvider>
                <MemoryRouter>
                  <Sidebar open onClose={vi.fn()} />
                </MemoryRouter>
              </TooltipProvider>
            </SidebarDataProvider>
          </QueryClientProvider>
        );
        const { rerender } = render(tree());
        fireEvent.focus(screen.getByRole("link", { name: "child_metadata" }));
        const tooltip = await screen.findByTestId("session-tooltip-content");
        expect(within(tooltip).getAllByTestId("session-tooltip-agent")[0]).toHaveTextContent(
          initialHarness === undefined ? /^Bundle · Claude SDK$/ : /^Bundle · Codex$/,
        );
        mockConversations([{ ...session, child_harness: undefined }]);
        rerender(tree());
        await waitFor(() =>
          expect(within(tooltip).getAllByTestId("session-tooltip-agent")[0]).toHaveTextContent(
            /^bundle$/,
          ),
        );
      },
    );

    it.each([false, true])(
      "renders an unresolved child after the real cache merge (project folder=%s)",
      async (inProject) => {
        agentsRef.current = [
          {
            id: "ag_bundle",
            name: "bundle",
            display_name: "Bundle",
            harness: "claude-sdk",
            description: null,
            skills: [],
          },
        ];
        const session = conv("child_legacy", "bundle", {
          agent_id: "ag_bundle",
          parent_session_id: "parent",
          updated_at: 100,
          labels: inProject ? { omni_project: "Alpha" } : {},
        });
        const client = new QueryClient();
        const queryKey = inProject ? ["project-sessions", "Alpha"] : ["conversations", "", false];
        client.setQueryData<ConversationsInfiniteData>(queryKey, {
          pages: [{ data: [session], first_id: session.id, last_id: session.id, has_more: false }],
          pageParams: [undefined],
        });
        function syncRows() {
          const rows = client.getQueryData<ConversationsInfiniteData>(queryKey)!.pages[0].data;
          mockConversations(inProject ? [] : rows);
          if (inProject) projectSessionsMock.current.Alpha = rows;
        }
        if (inProject) projectsMock.push("Alpha");
        syncRows();
        const tree = () => (
          <QueryClientProvider client={client}>
            <SidebarDataProvider>
              <TooltipProvider>
                <MemoryRouter>
                  <Sidebar open onClose={vi.fn()} />
                </MemoryRouter>
              </TooltipProvider>
            </SidebarDataProvider>
          </QueryClientProvider>
        );
        const { rerender } = render(tree());
        if (inProject) fireEvent.click(screen.getByRole("button", { name: /^Alpha/ }));
        fireEvent.focus(screen.getByRole("link", { name: "child_legacy" }));
        const tooltip = await screen.findByTestId("session-tooltip-content");
        const line = () => within(tooltip).getAllByTestId("session-tooltip-agent")[0];
        expect(line()).toHaveTextContent(/^Bundle · Claude SDK$/);
        const items = new Map([
          [session.id, nullsToUndefined({ id: session.id, child_harness: null })],
        ]);
        client.setQueryData<ConversationsInfiniteData>(
          queryKey,
          (cached) =>
            mergeItemsIntoPages(
              cached,
              items,
              { searchQuery: "", includeArchived: false },
              undefined,
            ).data,
        );
        syncRows();
        rerender(tree());
        await waitFor(() => expect(line()).toHaveTextContent(/^bundle$/));
        expect(line().querySelector(".lucide-bot")).not.toBeNull();
        expect(line().querySelector("img")).toBeNull();
        expect(
          client.getQueryData<ConversationsInfiniteData>(queryKey)!.pages[0].data[0].updated_at,
        ).toBe(session.updated_at);
      },
    );

    it("keeps the native wrapper icon, model and effort for an auto harness override", async () => {
      useHostsMock.mockReturnValue({
        data: [{ host_id: "host_model", name: "Laptop", status: "online" }],
      });
      const fetchSpy = vi.spyOn(identity, "authenticatedFetch").mockImplementation(
        async () =>
          new Response(
            JSON.stringify({
              models: [{ id: "opus[1m]", displayName: "Opus 5.5 (1M context)" }],
            }),
          ),
      );
      const tooltip = await hoverTooltip(
        conv("native_auto", "claude-native-ui", {
          host_id: "host_model",
          harness_override: AUTO_HARNESS_ID,
          llm_model: "opus[1m]",
          reasoning_effort: "medium",
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
        }),
      );
      const agentLine = within(tooltip).getAllByTestId("session-tooltip-agent")[0];
      await waitFor(() => expect(agentLine).toHaveTextContent(/^Opus 5.5 1M Medium$/));
      expect(agentLine.querySelector("[data-harness-icon='claude']")).not.toBeNull();
      const catalogCalls = fetchSpy.mock.calls.filter(([url]) =>
        String(url).includes("/model-options"),
      );
      expect(catalogCalls).toHaveLength(1);
      expect(String(catalogCalls[0][0])).toContain("/harnesses/claude-native/model-options");
    });

    it("shows unresolved Smart Routing for a non-native auto harness", async () => {
      agentsRef.current = [
        {
          id: "ag_polly",
          name: "polly",
          display_name: "Polly",
          harness: "pi",
          description: null,
          skills: [],
        },
      ];
      useHostsMock.mockReturnValue({
        data: [{ host_id: "host_model", name: "Laptop", status: "online" }],
      });
      const fetchSpy = vi.spyOn(identity, "authenticatedFetch");
      const tooltip = await hoverTooltip(
        conv("polly_auto", "polly", {
          agent_id: "ag_polly",
          host_id: "host_model",
          harness_override: AUTO_HARNESS_ID,
          llm_model: "model",
          reasoning_effort: "medium",
        }),
      );
      const agentLine = within(tooltip).getAllByTestId("session-tooltip-agent")[0];
      expect(agentLine).toHaveTextContent(new RegExp(`^Polly · ${SMART_ROUTING_LABEL}$`));
      expect(agentLine.querySelector("[data-harness-icon]")).toBeNull();
      expect(
        fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
      ).toHaveLength(0);
    });

    it("updates an open non-native routing tooltip when auto resolves to a concrete harness", async () => {
      agentsRef.current = [
        {
          id: "ag_polly",
          name: "polly",
          display_name: "Polly",
          harness: "pi",
          description: null,
          skills: [],
        },
      ];
      const session = conv("polly_routed", "polly", {
        agent_id: "ag_polly",
        harness_override: AUTO_HARNESS_ID,
        reasoning_effort: "medium",
        updated_at: 100,
      });
      mockConversations([session]);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const tree = () => (
        <QueryClientProvider client={client}>
          <SidebarDataProvider>
            <TooltipProvider>
              <MemoryRouter>
                <Sidebar open onClose={vi.fn()} />
              </MemoryRouter>
            </TooltipProvider>
          </SidebarDataProvider>
        </QueryClientProvider>
      );
      const { rerender } = render(tree());
      fireEvent.focus(screen.getByRole("link", { name: session.id }));
      const tooltip = await screen.findByTestId("session-tooltip-content");
      const agentLine = within(tooltip).getAllByTestId("session-tooltip-agent")[0];
      expect(agentLine).toHaveTextContent(new RegExp(`^Polly · ${SMART_ROUTING_LABEL}$`));

      mockConversations([{ ...session, harness_override: "codex" }]);
      rerender(tree());

      await waitFor(() => expect(agentLine).toHaveTextContent(/^Polly · Codex$/));
    });

    it.each([
      ["an agent-declared native harness", "custom-native", "Custom Native", true],
      ["only a canonical native agent name", "claude-native-ui", "claude-native-ui", false],
    ] as const)(
      "keeps auto unresolved with %s but no wrapper label",
      async (_evidence, name, displayName, declaredHarness) => {
        agentsRef.current = declaredHarness
          ? [
              {
                id: "ag_native",
                name,
                display_name: displayName,
                harness: "claude-native",
                description: null,
                skills: [],
              },
            ]
          : [];
        useHostsMock.mockReturnValue({
          data: [{ host_id: "host_model", name: "Laptop", status: "online" }],
        });
        const fetchSpy = vi
          .spyOn(identity, "authenticatedFetch")
          .mockImplementation(async () => new Response(JSON.stringify({ models: [] })));
        const tooltip = await hoverTooltip(
          conv("native_without_wrapper", name, {
            agent_id: "ag_native",
            host_id: "host_model",
            harness_override: AUTO_HARNESS_ID,
            llm_model: "opus[1m]",
            reasoning_effort: "medium",
          }),
        );
        const agentLine = within(tooltip).getAllByTestId("session-tooltip-agent")[0];
        expect(agentLine).toHaveTextContent(
          new RegExp(`^${displayName} · ${SMART_ROUTING_LABEL}$`),
        );
        expect(agentLine.querySelector(".lucide-bot")).not.toBeNull();
        expect(
          fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
        ).toHaveLength(0);
      },
    );

    it("refreshes an open tooltip after a parent-only metadata update", async () => {
      useHostsMock.mockReturnValue({
        data: [{ host_id: "host_model", name: "Laptop", status: "online" }],
      });
      const modelOptions = [
        {
          id: "codex-model",
          displayName: "Codex model",
          supportedReasoningEfforts: [],
        },
      ];
      const fetchSpy = vi
        .spyOn(identity, "authenticatedFetch")
        .mockImplementation(async () => new Response(JSON.stringify({ models: modelOptions })));
      const session = conv("parent_metadata", "codex-native-ui", {
        host_id: "host_model",
        llm_model: "codex-model",
        reasoning_effort: "medium",
        parent_session_id: null,
        labels: {},
        updated_at: 100,
      });
      const childSession = { ...session, parent_session_id: "parent_session" };
      function expectedAgentLine(row: Conversation) {
        const { label, effortLabel } = composerModelChipLabel({
          session: buildComposerSessionDescriptor(
            "codex-native",
            row.labels,
            row.parent_session_id,
          ),
          model: row.llm_model,
          modelOptions,
          modelSummary: "Codex model",
          effort: row.reasoning_effort,
        });
        return [label, effortLabel].filter(Boolean).join(" ");
      }
      expect(expectedAgentLine(session)).toBe("Codex model");
      expect(expectedAgentLine(childSession)).toBe("Codex model Medium");
      mockConversations([session]);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const onClose = vi.fn();
      const tree = () => (
        <QueryClientProvider client={client}>
          <SidebarDataProvider>
            <TooltipProvider>
              <MemoryRouter>
                <Sidebar open onClose={onClose} />
              </MemoryRouter>
            </TooltipProvider>
          </SidebarDataProvider>
        </QueryClientProvider>
      );
      const view = render(tree());
      fireEvent.focus(screen.getByRole("link", { name: session.id }));
      const tooltip = await screen.findByTestId("session-tooltip-content");
      const agentLine = within(tooltip).getAllByTestId("session-tooltip-agent")[0];
      await waitFor(() =>
        expect(agentLine).toHaveTextContent(new RegExp(`^${expectedAgentLine(session)}$`)),
      );
      mockConversations([childSession]);
      view.rerender(tree());
      expect(childSession.updated_at).toBe(session.updated_at);
      await waitFor(() =>
        expect(agentLine).toHaveTextContent(new RegExp(`^${expectedAgentLine(childSession)}$`)),
      );
      expect(
        fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
      ).toHaveLength(1);
      client.clear();
    });

    it("uses reachable brain-harness overrides for labels, icons and catalog keys", async () => {
      agentsRef.current = [
        {
          id: "ag_aria",
          name: "aria",
          display_name: "Aria",
          harness: "claude-sdk",
          description: null,
          skills: [],
        },
      ];
      useHostsMock.mockReturnValue({
        data: [{ host_id: "host_model", name: "Laptop", status: "online" }],
      });
      const fetchSpy = vi
        .spyOn(identity, "authenticatedFetch")
        .mockImplementation(async () => new Response(JSON.stringify({ models: [] })));
      const row = (id: string, harness: string) =>
        conv(id, "aria", {
          agent_id: "ag_aria",
          host_id: "host_model",
          llm_model: "model",
          reasoning_effort: "medium",
          harness_override: harness,
          labels: {},
        });
      mockConversations([row("brain_claude", "claude-sdk"), row("brain_codex", "codex")]);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(
        <QueryClientProvider client={client}>
          <SidebarDataProvider>
            <TooltipProvider>
              <MemoryRouter>
                <Sidebar open onClose={vi.fn()} />
              </MemoryRouter>
            </TooltipProvider>
          </SidebarDataProvider>
        </QueryClientProvider>,
      );
      async function checkRow(title: string, expected: string, harness: string, glyph: string) {
        const link = screen.getByRole("link", { name: title });
        fireEvent.focus(link);
        const tooltip = await screen.findByTestId("session-tooltip-content");
        const agentLine = within(tooltip).getAllByTestId("session-tooltip-agent")[0];
        expect(agentLine).toHaveTextContent(new RegExp(`^${expected}$`));
        expect(within(agentLine).getByTestId(glyph)).toBeInTheDocument();
        const queryKey = ["host-model-options", "host_model", harness];
        expect(client.getQueryCache().find({ queryKey, exact: true })?.queryKey).toEqual(queryKey);
        fireEvent.blur(link);
        await waitFor(() => expect(screen.queryByTestId("session-tooltip-content")).toBeNull());
      }
      await checkRow("brain_claude", "Aria · Claude SDK", "claude-sdk", "claude-glyph");
      await checkRow("brain_codex", "Aria · Codex", "codex", "codex-glyph");
      expect(
        client.getQueryCache().findAll({ queryKey: ["host-model-options", "host_model"] }),
      ).toHaveLength(2);
      expect(
        fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
      ).toHaveLength(0);
      client.clear();
    });

    it.each([false, true])(
      "uses the Codex model effort ladder after opening (hasEffort=%s)",
      async (hasEffort) => {
        useHostsMock.mockReturnValue({
          data: [{ host_id: "host_model", name: "Laptop", status: "online" }],
        });
        let resolveCatalog!: (response: Response) => void;
        const catalog = new Promise<Response>((resolve) => {
          resolveCatalog = resolve;
        });
        vi.spyOn(identity, "authenticatedFetch").mockImplementation((url) =>
          String(url).includes("/model-options")
            ? catalog
            : Promise.resolve(new Response(JSON.stringify({ data: [] }))),
        );
        const tooltip = await hoverTooltip(
          conv("codex_effort", "codex-native-ui", {
            host_id: "host_model",
            llm_model: "model",
            reasoning_effort: "medium",
            labels: { "omnigent.wrapper": "codex-native-ui" },
          }),
        );
        const agentLine = within(tooltip).getAllByTestId("session-tooltip-agent")[0];
        expect(agentLine).toHaveTextContent(/^Codex$/);
        await act(async () =>
          resolveCatalog(
            new Response(
              JSON.stringify({
                models: [
                  {
                    id: "model",
                    displayName: "Codex model",
                    supportedReasoningEfforts: hasEffort ? [{ reasoningEffort: "medium" }] : [],
                  },
                ],
              }),
            ),
          ),
        );
        await waitFor(() =>
          expect(agentLine).toHaveTextContent(hasEffort ? /^Codex model Medium$/ : /^Codex model$/),
        );
      },
    );

    it("shows a known non-native agent and harness without a model catalog", async () => {
      agentsRef.current = [
        {
          id: "ag_aria",
          name: "aria",
          display_name: "Aria",
          harness: "claude-sdk",
          description: null,
          skills: [],
        },
      ];
      const fetchSpy = vi.spyOn(identity, "authenticatedFetch");
      const tooltip = await hoverTooltip(
        conv("conv_known", "aria", { agent_id: "ag_aria", reasoning_effort: "high" }),
      );
      const agentLine = within(tooltip).getAllByTestId("session-tooltip-agent")[0];
      expect(agentLine).toHaveTextContent(/^Aria · Claude SDK$/);
      expect(
        fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
      ).toHaveLength(0);
      fetchSpy.mockRestore();
    });

    it("shows the unknown agent name without effort or model", async () => {
      const tooltip = await hoverTooltip(
        conv("conv_agent", "aria", {
          llm_model: "opus[1m]",
          reasoning_effort: "high",
        }),
      );
      expect(within(tooltip).getAllByTestId("session-tooltip-agent")[0]).toHaveTextContent(
        /^aria$/,
      );
      expect(
        within(tooltip).getAllByTestId("session-tooltip-agent")[0].querySelector(".lucide-bot"),
      ).not.toBeNull();
    });

    it("omits the agent line when the agent is unknown", async () => {
      const tooltip = await hoverTooltip(
        conv("conv_no_agent", "", { agent_name: null, workspace: "/srv/repo" }),
      );
      expect(within(tooltip).getByTestId("session-tooltip-cwd")).toBeInTheDocument();
      expect(within(tooltip).queryByTestId("session-tooltip-agent")).toBeNull();
    });

    it("shows a short working directory in full", async () => {
      const tooltip = await hoverTooltip(conv("conv_cwd", "aria", { workspace: "/srv/repo" }));
      const cwd = within(tooltip).getAllByTestId("session-tooltip-cwd")[0];
      expect(cwd.querySelector("[aria-hidden='true'].truncate")).toHaveTextContent(/^\/srv\/repo$/);
    });

    it("trims a long working directory from the left, keeping the last folder", async () => {
      // jsdom has no layout: model an 8px-per-character line in a 120px box.
      const scrollWidth = vi
        .spyOn(HTMLElement.prototype, "scrollWidth", "get")
        .mockImplementation(function (this: HTMLElement) {
          return (this.textContent?.length ?? 0) * 8;
        });
      const clientWidth = vi
        .spyOn(HTMLElement.prototype, "clientWidth", "get")
        .mockReturnValue(120);
      try {
        const workspace = "/Users/me/omnigent-worktrees/fix-sse";
        const tooltip = await hoverTooltip(conv("conv_long_cwd", "aria", { workspace }));
        const cwd = within(tooltip).getAllByTestId("session-tooltip-cwd")[0];
        await waitFor(() => {
          expect(cwd.querySelector("[aria-hidden='true'].truncate")).toHaveTextContent(
            /^…\/fix-sse$/,
          );
        });
        // Assistive tech still gets the untrimmed path.
        expect(cwd.querySelector(".sr-only")).toHaveTextContent(workspace);
      } finally {
        scrollWidth.mockRestore();
        clientWidth.mockRestore();
      }
    });

    it("omits the working-directory line when the workspace is unknown", async () => {
      const tooltip = await hoverTooltip(conv("conv_no_cwd", "aria", { workspace: null }));
      expect(within(tooltip).getByTestId("session-tooltip-agent")).toBeInTheDocument();
      expect(within(tooltip).queryByTestId("session-tooltip-cwd")).toBeNull();
    });

    it.each([
      {
        name: "Working",
        partial: { status: "running" as const },
        state: "running",
        key: "working",
      },
      { name: "Idle", partial: { status: "idle" as const }, state: "idle", key: "idle" },
      { name: "Error", partial: { status: "failed" as const }, state: "error", key: "error" },
      {
        name: "Needs response",
        partial: { status: "idle" as const, pending_elicitations_count: 1 },
        state: "awaiting",
        key: "needs-response",
      },
    ])("shows the $name status as the last line", async ({ name, partial, state, key }) => {
      const tooltip = await hoverTooltip(conv(`conv_status_${state}`, "aria", partial));
      const status = within(tooltip).getAllByTestId("session-tooltip-status")[0];
      expect(status).toHaveTextContent(new RegExp(`^${name}$`));
      expect(status).toHaveAttribute("data-state", key);
      if (state === "error") {
        expect(status).toHaveClass("text-destructive");
        expect(status.nextElementSibling).toBe(
          within(tooltip).getAllByTestId("session-tooltip-error")[0],
        );
      } else {
        expect(status.nextElementSibling?.getAttribute("data-testid")).not.toBe(
          "session-tooltip-error",
        );
      }
    });

    it("labels an unread session New messages, matching the row's unread dot", async () => {
      const session = conv("conv_status_unseen", "aria", { status: "idle", updated_at: 200 });
      seedReadState([{ id: session.id, viewer_last_seen: 199 }]);
      const tooltip = await hoverTooltip(session);
      const row = screen.getByRole("link", { name: /conv_status_unseen/ }).closest("li")!;
      expect(within(row).getByTestId("session-state-badge")).toHaveAttribute(
        "data-state",
        "unseen",
      );
      const status = within(tooltip).getAllByTestId("session-tooltip-status")[0];
      expect(status).toHaveTextContent(/^New messages$/);
      expect(status).toHaveAttribute("data-state", "new-messages");
    });

    it("shows Host disconnected when the latest item is a runner disconnect", async () => {
      const fetchPage = vi.spyOn(sessionsApi, "fetchSessionItemsPage").mockResolvedValue({
        items: [
          {
            id: "disconnect1",
            response_id: "response1",
            type: "error",
            status: "completed",
            source: "execution",
            code: "runner_disconnected",
            message: "Runner disconnected unexpectedly.",
          },
        ],
        hasMore: false,
      });
      try {
        const tooltip = await hoverTooltip(
          conv("conv_status_disconnected", "aria", { status: "idle" }),
        );
        await waitFor(() => {
          const status = within(tooltip).getAllByTestId("session-tooltip-status")[0];
          expect(status).toHaveTextContent(/^Host disconnected$/);
          expect(status).toHaveAttribute("data-state", "disconnected");
          expectStatusDot(status, "bg-muted-foreground");
        });
        expect(within(tooltip).queryByTestId("session-tooltip-error")).toBeNull();
      } finally {
        fetchPage.mockRestore();
      }
    });

    it("shows Starting up while the bound session is waking", async () => {
      useChatStore.setState({ conversationId: "conv_status_starting", status: "streaming" });
      const tooltip = await hoverTooltip(
        conv("conv_status_starting", "aria", { status: "failed" }),
      );
      const status = within(tooltip).getAllByTestId("session-tooltip-status")[0];
      expect(status).toHaveTextContent(/^Starting up$/);
      expect(within(tooltip).queryByTestId("session-tooltip-error")).toBeNull();
    });

    it("renders the persisted error message on its own line below Error", async () => {
      const message = "Codex exited — 403 budget limit reached for this workspace";
      const tooltip = await hoverTooltip(
        conv("conv_error_message", "aria", {
          status: "failed",
          labels: { "omnigent.last_task_error_message": message },
        }),
      );
      const status = within(tooltip).getAllByTestId("session-tooltip-status")[0];
      const error = within(tooltip).getAllByTestId("session-tooltip-error")[0];
      expect(status).toHaveTextContent(/^Error$/);
      expect(status.nextElementSibling).toBe(error);
      expect(error).toHaveTextContent(message);
      expect(error).toHaveClass("text-destructive", "line-clamp-3");
    });

    it("falls back to the generic error text without a persisted message", async () => {
      const tooltip = await hoverTooltip(conv("conv_error_fallback", "aria", { status: "failed" }));
      expect(within(tooltip).getAllByTestId("session-tooltip-error")[0]).toHaveTextContent(
        /^Latest message is an error$/,
      );
    });

    it.each([
      { state: "idle", partial: {}, token: "bg-popover-foreground/70" },
      {
        state: "needs-response",
        partial: { pending_elicitations_count: 1 },
        token: "bg-brand-accent",
      },
      { state: "error", partial: { status: "failed" as const }, token: "bg-destructive" },
    ])("draws $state as a theme-token dot", async ({ state, partial, token }) => {
      const tooltip = await hoverTooltip(conv(`conv_dot_${state}`, "aria", partial));
      const status = within(tooltip).getByTestId("session-tooltip-status");
      expect(status).toHaveAttribute("data-state", state);
      expectStatusDot(status, token);
    });

    it("draws New messages as a brand-accent dot like the row's unread dot", async () => {
      const session = conv("conv_dot_unseen", "aria", { status: "idle", updated_at: 200 });
      seedReadState([{ id: session.id, viewer_last_seen: 199 }]);
      const tooltip = await hoverTooltip(session);
      expectStatusDot(within(tooltip).getByTestId("session-tooltip-status"), "bg-brand-accent");
    });

    it("keeps the spinner for Working", async () => {
      const tooltip = await hoverTooltip(conv("conv_dot_working", "aria", { status: "running" }));
      const status = within(tooltip).getByTestId("session-tooltip-status");
      expect(within(status).getByTestId("running-dot")).toHaveClass("animate-spin");
    });

    it("shows the same details on a pinned, project-owned session", async () => {
      projectsMock.push("Customer X");
      seedPins(["conv_pinned_details"]);
      mockConversations([
        conv("conv_pinned_details", "aria", {
          status: "running",
          workspace: "/srv/repo",
          labels: { omni_project: "Customer X" },
        }),
      ]);
      renderSidebar();
      fireEvent.focus(screen.getByRole("link", { name: /conv_pinned_details/ }));
      const flyout = await screen.findByTestId("session-tooltip-content");
      expect(flyout).toHaveClass("w-72", "max-w-[calc(100vw-2rem)]");
      expect(within(flyout).getByText("Customer X")).toBeInTheDocument();
      expect(within(flyout).getByTestId("session-tooltip-agent")).toHaveTextContent(/^aria$/);
      expect(within(flyout).getByTestId("session-tooltip-cwd")).toHaveTextContent("/srv/repo");
      expect(within(flyout).getByTestId("session-tooltip-status")).toHaveTextContent(/^Working$/);
    });

    it("hides the project line on an unpinned row inside a project folder", async () => {
      projectsMock.push("Customer X");
      mockConversations([
        conv("project_unpinned", "aria", { labels: { omni_project: "Customer X" } }),
      ]);
      renderSidebar();
      fireEvent.click(screen.getByRole("button", { name: /^Customer X/ }));
      fireEvent.focus(screen.getByRole("link", { name: "project_unpinned" }));
      const tooltip = await screen.findByTestId("session-tooltip-content");
      expect(within(tooltip).queryByTestId("session-tooltip-project")).toBeNull();
      expect(tooltip).toHaveTextContent("project_unpinned");
    });

    it("hides unknown lines on a pinned session instead of filling them in", async () => {
      projectsMock.push("Customer X");
      seedPins(["conv_pinned_sparse"]);
      mockConversations([
        conv("conv_pinned_sparse", "", {
          agent_name: null,
          workspace: null,
          labels: { omni_project: "Customer X" },
        }),
      ]);
      renderSidebar();
      fireEvent.focus(screen.getByRole("link", { name: /conv_pinned_sparse/ }));
      const flyout = await screen.findByTestId("session-tooltip-content");
      expect(within(flyout).queryByTestId("session-tooltip-agent")).toBeNull();
      expect(within(flyout).queryByTestId("session-tooltip-cwd")).toBeNull();
      expect(within(flyout).getByTestId("session-tooltip-status")).toHaveTextContent(/^Idle$/);
    });
  });

  it("shares one hosts observer across multiple ordinary session rows", () => {
    const observerMounted = vi.fn();
    useHostsMock.mockImplementation(() => {
      useEffect(() => {
        observerMounted();
      }, []);
      return { data: [] };
    });
    mockConversations([
      conv("conv_one", "Codex", { host_id: "host_one" }),
      conv("conv_two", "Claude Code", { host_id: "host_two" }),
      conv("conv_three", "Codex", { host_id: "host_three" }),
    ]);

    renderSidebar();

    expect(screen.getByRole("link", { name: "conv_one" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "conv_two" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "conv_three" })).toBeInTheDocument();
    expect(observerMounted).toHaveBeenCalledTimes(1);
    expect(useHostsMock).toHaveBeenCalledWith({ includeSandbox: true });
  });

  it.each([
    {
      title: "resolved remote host name",
      hostId: "host_remote",
      hosts: [{ host_id: "host_remote", name: "Build Mac", sandbox_provider: null }],
      expected: "Build Mac",
    },
    {
      title: "sandbox provider label",
      hostId: "host_sandbox",
      hosts: [{ host_id: "host_sandbox", name: "Managed host", sandbox_provider: "modal" }],
      expected: "Modal Sandbox",
    },
    {
      title: "unknown host fallback",
      hostId: "host_missing",
      hosts: [],
      expected: "host_missing",
    },
  ])("shows the $title in the session tooltip", async ({ hostId, hosts, expected }) => {
    useHostsMock.mockReturnValue({ data: hosts });
    mockConversations([conv("conv_location", "Codex", { host_id: hostId })]);
    renderSidebar();

    fireEvent.pointerMove(screen.getByRole("link", { name: "conv_location" }), {
      pointerType: "mouse",
    });

    await waitFor(() => {
      expect(screen.getAllByTestId("session-tooltip-location")[0]).toHaveTextContent(expected);
    });
  });
});

describe("Sidebar failed session indicator", () => {
  function renderSessionSidebar(initialEntry = "/") {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = () => {
      const sidebar = <Sidebar open onClose={vi.fn()} />;
      return (
        <QueryClientProvider client={qc}>
          <SidebarDataProvider>
            <TooltipProvider>
              <MemoryRouter initialEntries={[initialEntry]}>
                <Routes>
                  <Route path="/c/:conversationId" element={sidebar} />
                  <Route path="*" element={sidebar} />
                </Routes>
              </MemoryRouter>
            </TooltipProvider>
          </SidebarDataProvider>
        </QueryClientProvider>
      );
    };
    const result = render(tree());
    return { ...result, refresh: () => result.rerender(tree()) };
  }

  it("replaces an unread dot with the error icon and keeps it after marking read", () => {
    const session = conv("conv_error", "Codex", {
      status: "failed",
      updated_at: 200,
    });
    seedReadState([{ id: session.id, viewer_last_seen: 199 }]);
    mockConversations([session]);
    renderSidebar();

    const row = screen.getByRole("link", { name: /conv_error/ }).closest("li")!;
    const badge = within(row).getByRole("img", { name: "Latest message is an error" });
    expect(badge).toHaveAttribute("data-state", "error");
    expect(badge.parentElement).toHaveClass("right-1", "w-6", "justify-center");
    expect(within(row).getByText("(unread)")).toBeInTheDocument();
    expect(within(row).queryByRole("img", { name: "New messages" })).toBeNull();

    act(() => markConversationSeen(session.id, session.updated_at));

    expect(within(row).queryByText("(unread)")).toBeNull();
    expect(within(row).getByRole("img", { name: "Latest message is an error" })).toBe(badge);
  });

  it("keeps the error icon on the active, already-read session", () => {
    const session = conv("conv_error", "Codex", {
      status: "failed",
      updated_at: 200,
    });
    seedReadState([{ id: session.id, viewer_last_seen: session.updated_at }]);
    mockConversations([session]);
    renderSessionSidebar(`/c/${session.id}`);

    const row = screen.getByRole("link", { name: "conv_error" }).closest("li")!;
    expect(within(row).queryByText("(unread)")).toBeNull();
    expect(within(row).getByRole("img", { name: "Latest message is an error" })).toHaveAttribute(
      "data-state",
      "error",
    );
  });

  it("updates a mounted row when only the session status changes", () => {
    // Stable project contexts keep unrelated context updates from bypassing
    // the row's render-field comparator.
    projectRowsRef.current = [];
    const session = conv("conv_error", "Codex", {
      status: "failed",
      updated_at: 200,
    });
    mockConversations([session]);
    const { refresh } = renderSessionSidebar();
    const row = screen.getByRole("link", { name: "conv_error" }).closest("li")!;
    expect(within(row).getByTestId("session-state-badge")).toHaveAttribute("data-state", "error");

    mockConversations([{ ...session, status: "idle" }]);
    refresh();

    expect(screen.getByRole("link", { name: "conv_error" }).closest("li")).toBe(row);
    expect(within(row).queryByTestId("session-state-badge")).toBeNull();

    mockConversations([{ ...session, status: "failed" }]);
    refresh();

    expect(screen.getByRole("link", { name: "conv_error" }).closest("li")).toBe(row);
    expect(within(row).getByTestId("session-state-badge")).toHaveAttribute("data-state", "error");
  });

  it.each([
    { status: "running" as const, pending: 0, expected: "running" },
    { status: "failed" as const, pending: 2, expected: "awaiting" },
  ])("lets $expected take precedence over a previous error", ({ status, pending, expected }) => {
    const session = conv("conv_error", "Codex", { status: "failed" });
    mockConversations([session]);
    const { refresh } = renderSessionSidebar();
    const row = screen.getByRole("link", { name: "conv_error" }).closest("li")!;
    expect(within(row).getByTestId("session-state-badge")).toHaveAttribute("data-state", "error");

    mockConversations([{ ...session, status, pending_elicitations_count: pending }]);
    refresh();

    expect(within(row).getByTestId("session-state-badge")).toHaveAttribute("data-state", expected);
    expect(within(row).queryByRole("img", { name: "Latest message is an error" })).toBeNull();
  });

  it.each([
    { status: "streaming" as const, terminalPending: false },
    { status: "idle" as const, terminalPending: true },
  ])("shows startup over a previous error for $status/$terminalPending", (startup) => {
    const wakingId = `conv_error_retry_${startup.status}_${startup.terminalPending}`;
    mockConversations([
      conv(wakingId, "Codex", {
        status: "failed",
        updated_at: 200,
      }),
      conv("conv_other", "Codex", { status: "failed" }),
    ]);
    seedReadState([{ id: wakingId, viewer_last_seen: 199 }]);
    useChatStore.setState({ conversationId: wakingId, ...startup });
    renderSidebar();

    const wakingRow = screen.getByRole("link", { name: new RegExp(wakingId) }).closest("li")!;
    expect(within(wakingRow).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "starting",
    );
    const otherRow = screen.getByRole("link", { name: "conv_other" }).closest("li")!;
    expect(within(otherRow).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "error",
    );
  });

  it("shows the error icon on a pinned session", () => {
    seedPins(["conv_error"]);
    mockConversations([conv("conv_error", "Codex", { status: "failed" })]);
    renderSidebar();

    const pinnedSection = screen.getByRole("button", { name: /^Pinned/ }).closest("section")!;
    const row = within(pinnedSection).getByRole("link", { name: "conv_error" }).closest("li")!;
    expect(within(row).getByRole("img", { name: "Latest message is an error" })).toHaveAttribute(
      "data-state",
      "error",
    );
  });

  describe.each(["regular", "pinned"] as const)("%s session error explanation", (surface) => {
    async function openExplanation(state: "error" | "running" | "awaiting" | "starting") {
      const id = `conv_hint_${surface}_${state}`;
      const pinned = surface === "pinned";
      if (pinned) {
        projectsMock.push("Customer X");
        seedPins([id]);
      }
      mockConversations([
        conv(id, "Codex", {
          status: state === "running" ? "running" : "failed",
          pending_elicitations_count: state === "awaiting" ? 1 : 0,
          labels: pinned ? { omni_project: "Customer X" } : {},
        }),
      ]);
      if (state === "starting") {
        useChatStore.setState({ conversationId: id, status: "streaming" });
      }
      renderSidebar();

      const row = screen.getByRole("link", { name: id });
      expect(within(row.closest("li")!).getByTestId("session-state-badge")).toHaveAttribute(
        "data-state",
        state,
      );
      if (pinned) fireEvent.focus(row);
      else fireEvent.pointerMove(row, { pointerType: "mouse" });
      return screen.findByTestId("session-tooltip-content");
    }

    it("explains the error through the row's existing hover surface", async () => {
      const content = await openExplanation("error");
      // Both surfaces label the state "Error" and explain it on the line below.
      const status = within(content).getByTestId("session-tooltip-status");
      expect(status).toHaveTextContent(/^Error$/);
      expect(status).toHaveClass("text-destructive");
      expect(status.querySelector("span.rounded-full")).toHaveClass("bg-destructive");
      expect(status.nextElementSibling).toBe(within(content).getByTestId("session-tooltip-error"));
      expect(status.nextElementSibling).toHaveTextContent("Latest message is an error");
    });

    it.each(["running", "awaiting", "starting"] as const)(
      "omits an old error explanation while the session is %s",
      async (state) => {
        const content = await openExplanation(state);
        expect(content).not.toHaveTextContent("Latest message is an error");
      },
    );
  });
});

describe("Sidebar idle latest-message error", () => {
  beforeEach(() => {
    agentsRef.current = [];
    vi.spyOn(sessionsApi, "fetchSessionItemsPage").mockResolvedValue({
      items: [
        {
          id: "native_error",
          response_id: "response1",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "output_text", text: "API Error: Request rejected (429)" }],
        },
      ],
      hasMore: true,
    });
  });
  afterEach(() => vi.mocked(sessionsApi.fetchSessionItemsPage).mockRestore());

  it("shows the icon for an unopened idle session and keeps it after marking read", async () => {
    const session = conv("conv_idle_error", "Claude Code", { status: "idle", updated_at: 200 });
    seedReadState([{ id: session.id, viewer_last_seen: 199 }]);
    mockConversations([session]);
    renderSidebar();
    const row = screen.getByRole("link", { name: /conv_idle_error/ }).closest("li")!;
    const badge = await within(row).findByRole("img", { name: "Latest message is an error" });
    expect(badge).toHaveAttribute("data-state", "error");
    expect(badge.querySelector("svg")).toHaveClass("text-destructive", "size-3.5");
    expect(within(row).getByText("(unread)")).toBeInTheDocument();
    act(() => markConversationSeen(session.id, session.updated_at));
    expect(within(row).queryByText("(unread)")).toBeNull();
    expect(within(row).getByRole("img", { name: "Latest message is an error" })).toBe(badge);
    expect(sessionsApi.fetchSessionItemsPage).toHaveBeenCalledTimes(1);
  });

  it("shows an idle message error on a collapsed project's marker", async () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_idle_error", "Claude Code", {
        status: "idle",
        labels: { omni_project: "Customer X" },
      }),
    ]);
    renderSidebar();
    const header = screen.getByRole("button", { name: /^Customer X/ });
    await within(header).findByRole("img", { name: "Latest message is an error" });
    fireEvent.click(header);
    const row = screen.getByRole("link", { name: "conv_idle_error" }).closest("li")!;
    expect(
      within(row).getByRole("img", { name: "Latest message is an error" }),
    ).toBeInTheDocument();
    expect(sessionsApi.fetchSessionItemsPage).toHaveBeenCalledTimes(1);
  });
});

// Sidebar grouping: the viewer's own sessions ("My sessions" tab) keep the
// Pinned / Projects / Sessions structure; sessions shared with the viewer live
// on a separate "Shared with me" tab. "Shared" = sessions whose `owner` is
// another user; a null/absent owner is the viewer's own (single-user / legacy).
// In tests the resolved viewer id is null, so any non-null owner reads as shared.
describe("Sidebar sections", () => {
  it("splits owned and shared sessions across the My / Shared filters", () => {
    mockConversations([
      conv("conv_mine_legacy", "Claude Code"), // owner absent = owned
      conv("conv_mine_acl", "Claude Code", { owner: null }),
      conv("conv_shared", "Claude Code", { owner: "other@example.com" }),
    ]);
    renderSidebar();

    // "All sessions": everything the viewer can see.
    selectSessionFilter("all");
    const recentSection = screen.getByText("Sessions").closest("section")!;
    expect(within(recentSection).getByText("conv_mine_legacy")).toBeInTheDocument();
    expect(within(recentSection).getByText("conv_mine_acl")).toBeInTheDocument();
    expect(within(recentSection).getByText("conv_shared")).toBeInTheDocument();

    // "My sessions": owned only, no shared one leaking in (which would make the
    // viewer think they own it).
    selectSessionFilter("mine");
    expect(screen.getByText("conv_mine_legacy")).toBeInTheDocument();
    expect(screen.getByText("conv_mine_acl")).toBeInTheDocument();
    expect(screen.queryByText("conv_shared")).toBeNull();

    // "Shared sessions": only the shared one, owned ones hidden.
    showSharedTab();
    expect(screen.getByText("conv_shared")).toBeInTheDocument();
    expect(screen.queryByText("conv_mine_legacy")).toBeNull();
    expect(screen.queryByText("conv_mine_acl")).toBeNull();
  });

  it("titles the baseline list Sessions even with no sibling group", () => {
    mockConversations([conv("conv_only_mine", "Claude Code")]);
    renderSidebar();
    // "Sessions" always renders so the list is labeled (and collapsible)
    // from the first session; the project group stays hidden when empty.
    expect(screen.getByText("conv_only_mine")).toBeInTheDocument();
    expect(screen.getByText("Sessions")).toBeInTheDocument();
    // With no shared sessions, the Shared tab shows its empty state rather
    // than any session rows.
    showSharedTab();
    expect(screen.getAllByText("No sessions")[0]).toBeInTheDocument();
    expect(screen.queryByText("conv_only_mine")).toBeNull();
  });
});

// The sidebar splits sessions across two tabs: "My sessions" (owned, with the
// full Pinned / Projects / Chats structure) and "Shared with me" (a flat list).
describe("Sidebar tabs", () => {
  it("keeps New session visible on both tabs and snaps back to My sessions when used", () => {
    mockConversations([
      conv("conv_mine", "Claude Code"),
      conv("conv_shared", "Claude Code", { owner: "other@example.com" }),
    ]);
    renderSidebar();
    expect(screen.getByTestId("new-chat-button")).toBeInTheDocument();

    // New session always creates a session the viewer owns, so it stays
    // reachable on the Shared tab too (not hidden).
    showSharedTab();
    expect(screen.getByTestId("new-chat-button")).toBeInTheDocument();
    expect(screen.getByText("conv_shared")).toBeInTheDocument();

    // Using it flips back to "My sessions": the shared row hides and the owned
    // one returns.
    fireEvent.click(screen.getByTestId("new-chat-button"));
    expect(screen.getByText("conv_mine")).toBeInTheDocument();
    expect(screen.queryByText("conv_shared")).toBeNull();
  });

  it("drops the Shared filter option on a single-user (local) server", () => {
    // A loopback-only server can't share sessions, so that one option is
    // meaningless there. The rest of the menu keeps working.
    isServerLocalMock.mockReturnValue(true);
    mockConversations([
      conv("conv_mine", "Claude Code"),
      conv("conv_done", "Claude Code", { archived: true }),
    ]);
    renderSidebar();

    fireEvent.pointerDown(screen.getByTestId("session-filter"), {
      button: 0,
      ctrlKey: false,
      pointerType: "mouse",
    });
    expect(screen.queryByTestId("session-filter-shared")).toBeNull();
    expect(screen.getByTestId("session-filter-all")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("session-filter-archived"));

    // Picking a filter still re-scopes the list here — the local-server case
    // must not pin the scope and swallow the choice.
    expect(screen.getByText("conv_done")).toBeInTheDocument();
    expect(screen.queryByText("conv_mine")).toBeNull();
  });

  it("shows every pinned session in Pinned regardless of the My/Shared filter", () => {
    // Pins are ownership-agnostic, so the Pinned section always includes all
    // pinned sessions — an owned pin stays visible on the Shared tab and a
    // shared pin stays visible on My sessions. Only the unpinned rows re-scope
    // with the filter.
    mockConversations([
      conv("conv_mine", "Claude Code"),
      conv("conv_shared", "Claude Code", { owner: "other@example.com" }),
    ]);
    seedPins(["conv_mine", "conv_shared"]);
    renderSidebar();

    // "My sessions": both pins show under Pinned even though conv_shared is not
    // owned by the viewer.
    selectSessionFilter("mine");
    const minePinned = screen.getByText("Pinned").closest("section")!;
    expect(within(minePinned).getByText("conv_mine")).toBeInTheDocument();
    expect(within(minePinned).getByText("conv_shared")).toBeInTheDocument();

    // "Shared sessions": both pins still show under Pinned even though
    // conv_mine is owned by the viewer.
    showSharedTab();
    const sharedPinned = screen.getByText("Pinned").closest("section")!;
    expect(within(sharedPinned).getByText("conv_mine")).toBeInTheDocument();
    expect(within(sharedPinned).getByText("conv_shared")).toBeInTheDocument();

    // "Archived sessions": the (non-archived) pins still show under Pinned —
    // switching to Archived doesn't empty the section.
    selectSessionFilter("archived");
    const archivedPinned = screen.getByText("Pinned").closest("section")!;
    expect(within(archivedPinned).getByText("conv_mine")).toBeInTheDocument();
    expect(within(archivedPinned).getByText("conv_shared")).toBeInTheDocument();
  });

  it("keeps the Projects section and its folders on the Shared tab", () => {
    // Projects are ownership-agnostic like pins: the Projects group and its
    // folders always render, unaffected by the filter, so a filed session
    // stays in its folder on the Shared tab rather than the folder vanishing.
    projectsMock.push("Alpha");
    mockConversations([
      conv("conv_mine", "Claude Code", { labels: { omni_project: "Alpha" } }),
      conv("conv_shared", "Claude Code", { owner: "other@example.com" }),
    ]);
    renderSidebar();

    // My sessions tab carries the Projects group.
    expect(screen.getByText("Projects")).toBeInTheDocument();

    // Shared tab: the Projects group still renders with its Alpha folder, and
    // the shared session shows in Sessions.
    showSharedTab();
    expect(screen.getByText("Projects")).toBeInTheDocument();
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("conv_shared")).toBeInTheDocument();
  });

  it("keeps a shared session in the flat list on a project-name collision", () => {
    // Filing is owner-only (UNLIKE pins), so `projectGroups` membership is
    // ownership-gated. A session shared with the viewer that carries its own
    // owner's `omni_project` label colliding with one of the viewer's folder
    // names must NOT be treated as filed — otherwise `filedIds` would swallow
    // it and it would vanish from the flat Sessions list. (The folder's own
    // expanded contents come from the owner-scoped `?project=` server fetch,
    // so this asserts the parent-level flat-list scoping the fix changed.)
    projectsMock.push("Alpha");
    mockConversations([
      conv("conv_owned", "Claude Code", { labels: { omni_project: "Alpha" } }),
      // Shared (owner set) with the SAME project-name label — the collision.
      conv("conv_shared_alpha", "Claude Code", {
        owner: "other@example.com",
        labels: { omni_project: "Alpha" },
      }),
    ]);
    renderSidebar();

    // The shared session shows on "All sessions" (the default "My sessions" tab
    // scopes it out). The owned session is filed (peeled out of the flat list
    // into its collapsed folder), but the shared collision stays in the flat
    // Sessions list rather than being pulled into the viewer's folder.
    selectSessionFilter("all");
    const sessionsSection = screen.getByText("Sessions").closest("section")!;
    expect(within(sessionsSection).queryByText("conv_owned")).toBeNull();
    expect(within(sessionsSection).getByText("conv_shared_alpha")).toBeInTheDocument();
  });

  it("keeps paginating when the shared tab is empty on the loaded page but more exist", () => {
    // The list is one paginated stream (owned + shared mixed, updated_at desc),
    // so page 1 can be all-owned while shared sessions live on a later page.
    // The Shared tab must keep its pagination sentinel mounted rather than
    // stranding the user on a false "empty" state.
    let observerCallback: IntersectionObserverCallback | undefined;
    class TestObserver {
      constructor(cb: IntersectionObserverCallback) {
        observerCallback = cb;
      }
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
      takeRecords = () => [];
      root = null;
      rootMargin = "";
      thresholds = [];
    }
    vi.stubGlobal("IntersectionObserver", TestObserver);

    const fetchNextPage = vi.fn();
    const rows = [conv("conv_mine", "Claude Code")]; // owned only on this page
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

    showSharedTab();
    // False-empty on the loaded window, but the sentinel is still mounted.
    expect(screen.getAllByText("No sessions")[0]).toBeInTheDocument();
    observerCallback?.(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );
    expect(fetchNextPage).toHaveBeenCalled();
  });
});

// Section headers double as collapse toggles, persisted to localStorage so
// the preference survives reloads (same contract as pins).
describe("Sidebar collapsible sections", () => {
  it("collapses a section on header click and persists across remount", () => {
    mockConversations([conv("conv_mine", "Claude Code"), conv("conv_mine_two", "Claude Code")]);
    renderSidebar();

    // Collapse hides the section's rows but keeps the header — a vanished
    // header would strand the user with no way to expand again.
    fireEvent.click(screen.getByRole("button", { name: "Sessions" }));
    expect(screen.queryByText("conv_mine")).toBeNull();
    expect(screen.queryByText("conv_mine_two")).toBeNull();
    expect(screen.getByRole("button", { name: "Sessions" })).toBeInTheDocument();

    // Fresh mount re-reads localStorage: still collapsed. If this fails,
    // the toggle wrote state only to memory and reloads lose it.
    cleanup();
    renderSidebar();
    expect(screen.queryByText("conv_mine")).toBeNull();

    // Expanding brings the rows back.
    fireEvent.click(screen.getByRole("button", { name: "Sessions" }));
    expect(screen.getByText("conv_mine")).toBeInTheDocument();
  });
});

// Pagination belongs to the Sessions list: collapsing it must take the
// "Load more" button with it, or the button floats under nothing.
describe("Sidebar load-more vs collapsed Sessions", () => {
  it("hides Load more while Sessions is collapsed and restores it on expand", () => {
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
    fireEvent.click(screen.getByRole("button", { name: "Sessions" }));
    // Collapsed Sessions hides its rows AND the pagination affordance.
    expect(screen.queryByText("conv_mine")).toBeNull();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Sessions" }));
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
  });

  it("auto-fetches the next page when the sentinel scrolls into view (infinite scroll)", () => {
    // Start on the "all" tab so the background all-sessions paginator doesn't
    // fire on mount (it only runs on "mine"/"shared" tabs). The default tab is
    // "mine" (DEFAULT_SESSION_FILTER), so force "all" via localStorage before
    // rendering. This isolates the test to sentinel-triggered pagination only.
    localStorage.setItem("omnigent:session-filter", "all");

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
    expect(fetchNextPage).toHaveBeenCalledTimes(2);

    vi.unstubAllGlobals();
  });
});

// The sidebar uses two queries: an all-sessions query for inbox/WS and a
// tab-scoped query for display on the "mine" and "shared" tabs. The tab-scoped
// query passes `visibility` to the server so the server paginates only the
// relevant sessions (proper fix for OMNI-6002).
describe("Sidebar visibility filter (server-side mine/shared split)", () => {
  it('calls useConversations with visibility="mine" when on the mine tab', () => {
    mockConversations([conv("conv_mine", "Claude Code")]);
    renderSidebar();

    // Sidebar always renders on the "mine" tab by default. Check that one of the
    // useConversations calls passes visibility="mine" (the tab-scoped query).
    const calls = useConvMock.mock.calls;
    const mineCall = calls.find((args) => args[4] === "mine");
    expect(mineCall).toBeDefined();
    // The tab-scoped query uses includeArchived=false and enabled=true.
    expect(mineCall![1]).toBe(false);
    expect((mineCall![2] as { enabled?: boolean }).enabled).toBe(true);
  });

  it('calls useConversations with visibility="shared" when on the shared tab', () => {
    mockConversations([conv("conv_mine", "Claude Code")]);
    renderSidebar();
    selectSessionFilter("shared");

    const calls = useConvMock.mock.calls;
    const sharedCall = calls.find((args) => args[4] === "shared");
    expect(sharedCall).toBeDefined();
    expect(sharedCall![1]).toBe(false);
    expect((sharedCall![2] as { enabled?: boolean }).enabled).toBe(true);
  });

  it("disables the tab-scoped query when on the all tab", () => {
    mockConversations([conv("conv_mine", "Claude Code")]);
    renderSidebar();
    selectSessionFilter("all");

    const calls = useConvMock.mock.calls;
    // No call should pass visibility="mine" or "shared" — the tab-scoped query
    // is disabled (enabled=false) when the active tab is "all".
    const disabledTabCalls = calls.filter(
      (args) => (args[2] as { enabled?: boolean })?.enabled === false,
    );
    expect(disabledTabCalls.length).toBeGreaterThan(0);
  });

  it("renders sessions from the tab-scoped query on the mine tab", () => {
    // When on the "mine" tab the display query is the filtered one. Both calls
    // return the same mock data here, so the visible session row reflects the
    // tab-scoped result.
    mockConversations([conv("conv_mine", "Claude Code")]);
    renderSidebar();

    expect(screen.getByText("conv_mine")).toBeInTheDocument();
  });
});

// Project feature: sessions carrying a project label are peeled out of the
// "Sessions" list into a folder under the "Projects" group (rendered between
// Pinned and Sessions). The project list comes from useProjects() (mocked here).
describe("Sidebar project sections", () => {
  it("groups sessions by their project label, separate from Sessions", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_unfiled", "Claude Code"),
      conv("conv_filed", "Claude Code", { labels: { omni_project: "Customer X" } }),
    ]);
    renderSidebar();

    // projects default collapsed, so the row is hidden until the header is
    // clicked. The unfiled session stays visible in Sessions regardless.
    const recentSection = screen.getByText("Sessions").closest("section")!;
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

  it("shows a window member inside the folder even when the folder's own fetch lacks it", () => {
    // The optimistic-move frame: the row already carries the folder's
    // first-class id in the loaded window (useMoveToProject's overlay), but
    // the folder's own fetch answered before the PATCH committed and lacks
    // it. The folder body unions both sources, so the just-moved row is
    // visible immediately instead of waiting out the PATCH + refetch chain.
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_unfiled", "Claude Code"),
      conv("conv_moved", "Claude Code", { project_id: "p_Customer X" }),
    ]);
    projectSessionsMock.current["Customer X"] = [];
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /^Customer X/ }));
    const projectSection = screen.getByText("Customer X").closest("section")!;
    expect(within(projectSection).getByText("conv_moved")).toBeInTheDocument();
    // Grouped out of the flat Sessions list, not duplicated there.
    const recentSection = screen.getByText("Sessions").closest("section")!;
    expect(within(recentSection).queryByText("conv_moved")).toBeNull();
  });

  it("closes the mobile overlay when New session is selected from the project menu", async () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_filed", "Claude Code", { labels: { omni_project: "Customer X" } }),
    ]);
    const onClose = vi.fn();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/"]}>
              <Sidebar open onClose={onClose} />
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </QueryClientProvider>,
    );

    fireEvent.pointerDown(screen.getByTestId("project-actions"), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByTestId("project-new-session-menu"));
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
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/c/conv_filed"]}>
              <Routes>
                <Route path="/c/:conversationId" element={<Sidebar open onClose={vi.fn()} />} />
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
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
    seedPins(["conv_pinned"]);
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
    // in Sessions — projects are driven by the project list, not the labels alone.
    mockConversations([conv("conv_filed", "Claude Code", { labels: { omni_project: "Ghost" } })]);
    renderSidebar();

    expect(screen.queryByText("Ghost")).toBeNull();
    const recentSection = screen.getByText("Sessions").closest("section")!;
    expect(within(recentSection).getByText("conv_filed")).toBeInTheDocument();
  });

  it("expands all project folders at once, then hides the now-redundant control", () => {
    projectsMock.push("Alpha", "Beta");
    mockConversations([
      conv("conv_a", "Claude Code", { labels: { omni_project: "Alpha" } }),
      conv("conv_b", "Claude Code", { labels: { omni_project: "Beta" } }),
    ]);
    renderSidebar();

    // Folders default collapsed, so "expand all" is offered up front.
    expect(screen.getByRole("button", { name: /^Alpha/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByRole("button", { name: /^Beta/ })).toHaveAttribute("aria-expanded", "false");

    // Open just one folder, then expand all → every folder opens. Expand-all
    // lives in the Projects header kebab, so open it before clicking.
    fireEvent.click(screen.getByRole("button", { name: /^Alpha/ }));
    openProjectsMenu();
    fireEvent.click(screen.getByTestId("expand-all-projects"));
    expect(screen.getByRole("button", { name: /^Alpha/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /^Beta/ })).toHaveAttribute("aria-expanded", "true");

    // With everything open it would be a no-op, so only Collapse all remains.
    openProjectsMenu();
    expect(screen.queryByTestId("expand-all-projects")).toBeNull();
    expect(screen.getByTestId("collapse-all-projects")).toBeInTheDocument();
  });

  it("offers both Expand all and Collapse all while folders are in mixed states", () => {
    projectsMock.push("Alpha", "Beta");
    mockConversations([
      conv("conv_a", "Claude Code", { labels: { omni_project: "Alpha" } }),
      conv("conv_b", "Claude Code", { labels: { omni_project: "Beta" } }),
    ]);
    renderSidebar();

    // Nothing open: only Expand all applies — there's nothing to collapse.
    openProjectsMenu();
    expect(screen.getByTestId("expand-all-projects")).toBeInTheDocument();
    expect(screen.queryByTestId("collapse-all-projects")).toBeNull();
    closeProjectsMenu();

    // One of two open (mixed): both are offered, so a partly-open set can be
    // closed in one go without expanding everything first.
    fireEvent.click(screen.getByRole("button", { name: /^Alpha/ }));
    openProjectsMenu();
    expect(screen.getByTestId("expand-all-projects")).toBeInTheDocument();
    expect(screen.getByTestId("collapse-all-projects")).toBeInTheDocument();

    // Collapse all closes every folder outright.
    fireEvent.click(screen.getByTestId("collapse-all-projects"));
    expect(screen.getByRole("button", { name: /^Alpha/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByRole("button", { name: /^Beta/ })).toHaveAttribute("aria-expanded", "false");
  });

  it("collapses every folder regardless of how they were opened", () => {
    projectsMock.push("Alpha", "Beta");
    mockConversations([
      conv("conv_a", "Claude Code", { labels: { omni_project: "Alpha" } }),
      conv("conv_b", "Claude Code", { labels: { omni_project: "Beta" } }),
    ]);
    renderSidebar();

    // Opened by hand rather than via Expand all — Collapse all still applies.
    fireEvent.click(screen.getByRole("button", { name: /^Alpha/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Beta/ }));
    openProjectsMenu();
    expect(screen.queryByTestId("expand-all-projects")).toBeNull();

    fireEvent.click(screen.getByTestId("collapse-all-projects"));
    expect(screen.getByRole("button", { name: /^Alpha/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByRole("button", { name: /^Beta/ })).toHaveAttribute("aria-expanded", "false");

    // Nothing left to collapse, so only Expand all is offered again.
    openProjectsMenu();
    expect(screen.getByTestId("expand-all-projects")).toBeInTheDocument();
    expect(screen.queryByTestId("collapse-all-projects")).toBeNull();
  });

  it("hides the expand-all control while the Projects group is collapsed", () => {
    // With the whole "Projects" group folded, its folders aren't rendered, so
    // expand-all / revert would be a no-op — the control must not show.
    projectsMock.push("Alpha", "Beta");
    mockConversations([
      conv("conv_a", "Claude Code", { labels: { omni_project: "Alpha" } }),
      conv("conv_b", "Claude Code", { labels: { omni_project: "Beta" } }),
    ]);
    renderSidebar();

    // Offered (in the header kebab) while the group is expanded (default).
    openProjectsMenu();
    expect(screen.getByTestId("expand-all-projects")).toBeInTheDocument();
    closeProjectsMenu();

    // Collapse the "Projects" group → control disappears from the kebab.
    fireEvent.click(screen.getByRole("button", { name: "Projects" }));
    openProjectsMenu();
    expect(screen.queryByTestId("expand-all-projects")).toBeNull();
    expect(screen.queryByTestId("collapse-all-projects")).toBeNull();
    closeProjectsMenu();

    // Re-expanding the group brings it back.
    fireEvent.click(screen.getByRole("button", { name: "Projects" }));
    openProjectsMenu();
    expect(screen.getByTestId("expand-all-projects")).toBeInTheDocument();
  });

  it("enters session-selection mode from the Projects header kebab", () => {
    projectsMock.push("Alpha");
    mockConversations([
      conv("conv_a", "Claude Code", { labels: { omni_project: "Alpha" } }),
      conv("conv_loose", "Claude Code"),
    ]);
    renderSidebar();

    // Not selecting yet: the bulk-action bar's exit control is absent.
    expect(screen.queryByRole("button", { name: "Exit selection mode" })).toBeNull();

    // "Select sessions" in the Projects kebab flips the whole sidebar into
    // selection mode (same mode the Sessions-header trigger opens).
    openProjectsMenu();
    fireEvent.click(screen.getByTestId("projects-select-sessions"));
    expect(screen.getByRole("button", { name: "Exit selection mode" })).toBeInTheDocument();
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
    // delete with the folder's { id, name }.
    expect(screen.getByText(/all of its sessions/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));
    expect(deleteProjectSpy).toHaveBeenCalledWith(
      { id: "p_Customer X", name: "Customer X" },
      expect.anything(),
    );
  });

  it("offers a desktop New session shortcut while keeping the touch menu accessible", async () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_filed", "Claude Code", { labels: { omni_project: "Customer X" } }),
    ]);
    renderSidebar();

    const shortcut = screen.getByRole("link", { name: "New session in Customer X" });
    expect(shortcut).toHaveAttribute("href", "/?project=Customer%20X");
    expect(shortcut).toHaveClass("hidden", "[@media((hover:hover)_and_(pointer:fine))]:flex");
    const menuButton = screen.getByTestId("project-actions");
    expect(menuButton).not.toHaveClass("hidden");
    expect(menuButton).not.toHaveClass("sr-only");
    fireEvent.pointerDown(screen.getByRole("button", { name: "Project actions for Customer X" }), {
      button: 0,
      ctrlKey: false,
    });
    const menuItem = await screen.findByTestId("project-new-session-menu");
    expect(menuItem).not.toHaveClass("hidden");
    expect(menuItem).toHaveClass("[@media((hover:hover)_and_(pointer:fine))]:hidden");
    expect(menuItem.closest("a")).toHaveAttribute("href", "/?project=Customer%20X");
  });

  it("reveals project actions on hover only on wide fine-pointer layouts", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_running", "Claude Code", {
        labels: { omni_project: "Customer X" },
        status: "running",
      }),
    ]);
    renderSidebar();

    const kebab = screen.getByTestId("project-actions");
    const revealWrapper = kebab.closest("div[class*=transition-opacity]")!;
    expect(revealWrapper).toHaveClass(
      "[@media((hover:hover)_and_(pointer:fine))]:md:opacity-0",
      "[@media((hover:hover)_and_(pointer:fine))]:md:group-hover/header:opacity-100",
    );
    const outerBox = kebab.closest("div[class*=absolute]")!;
    expect(outerBox).toHaveClass(
      "[@media((hover:hover)_and_(pointer:fine))]:md:pointer-events-none",
      "[@media((hover:hover)_and_(pointer:fine))]:md:group-hover/header:pointer-events-auto",
    );
  });
});

// A collapsed project bubbles up its hidden rows' marker, using the same
// SessionStateBadge a row shows. Only while collapsed.
describe("Sidebar collapsed project marker", () => {
  it("surfaces an error ahead of unread messages and moves it to the expanded row", () => {
    projectsMock.push("Customer X");
    seedReadState([{ id: "conv_unread", viewer_last_seen: 199 }]);
    mockConversations([
      conv("conv_error", "Codex", {
        labels: { omni_project: "Customer X" },
        status: "failed",
      }),
      conv("conv_unread", "Codex", {
        labels: { omni_project: "Customer X" },
        status: "idle",
        updated_at: 200,
      }),
    ]);
    renderSidebar();

    const header = screen.getByRole("button", { name: /^Customer X/ });
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(within(header).getByRole("img", { name: "Latest message is an error" })).toHaveAttribute(
      "data-state",
      "error",
    );

    fireEvent.click(header);

    expect(header).toHaveAttribute("aria-expanded", "true");
    expect(within(header).queryByTestId("session-state-badge")).toBeNull();
    const row = screen.getByRole("link", { name: "conv_error" }).closest("li")!;
    expect(
      within(row).getByRole("img", { name: "Latest message is an error" }),
    ).toBeInTheDocument();
  });

  it.each(["failed", "idle"] as const)(
    "prioritizes a running session over an unread $0 sibling",
    (status) => {
      projectsMock.push("Customer X");
      seedReadState([
        { id: "conv_other", viewer_last_seen: 199 },
        { id: "conv_running", viewer_last_seen: 199 },
      ]);
      mockConversations([
        conv("conv_other", "Codex", {
          labels: { omni_project: "Customer X" },
          status,
          updated_at: 200,
        }),
        conv("conv_running", "Codex", {
          labels: { omni_project: "Customer X" },
          status: "running",
          updated_at: 200,
        }),
      ]);
      renderSidebar();

      const header = screen.getByRole("button", { name: /^Customer X/ });
      expect(within(header).getByTestId("session-state-badge")).toHaveAttribute(
        "data-state",
        "running",
      );
      expect(within(header).queryByRole("img", { name: "Latest message is an error" })).toBeNull();
    },
  );

  it.each([
    { status: "streaming" as const, terminalPending: false },
    { status: "idle" as const, terminalPending: true },
  ])("prioritizes startup over a previous error for $status/$terminalPending", (startup) => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_error", "Codex", {
        labels: { omni_project: "Customer X" },
        status: "failed",
      }),
    ]);
    useChatStore.setState({ conversationId: "conv_error", ...startup });
    renderSidebar();

    const header = screen.getByRole("button", { name: /^Customer X/ });
    expect(within(header).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "starting",
    );

    act(() => useChatStore.setState({ status: "idle", terminalPending: false }));
    expect(within(header).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "error",
    );
  });

  it("prioritizes a pending approval over running and errored sessions", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_error", "Codex", {
        labels: { omni_project: "Customer X" },
        status: "failed",
      }),
      conv("conv_awaiting", "Codex", {
        labels: { omni_project: "Customer X" },
        pending_elicitations_count: 2,
      }),
      conv("conv_running", "Codex", {
        labels: { omni_project: "Customer X" },
        status: "running",
      }),
    ]);
    renderSidebar();

    const header = screen.getByRole("button", { name: /^Customer X/ });
    expect(within(header).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "awaiting",
    );
    expect(
      within(header).getByRole("img", { name: "2 approval prompts waiting" }),
    ).toBeInTheDocument();
    expect(within(header).queryByRole("img", { name: "Latest message is an error" })).toBeNull();
  });

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

  // A dot/spinner marker on a collapsed project must sit in the same size-6
  // centered slot as a row's badge (pulled to the right-1 edge, offsetting the
  // folder button's px-2), so the dots line up vertically down the sidebar
  // instead of the folder dot drifting right of the rows above it.
  it("aligns a collapsed-project dot marker with the row badge slot", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_running", "Claude Code", {
        labels: { omni_project: "Customer X" },
        status: "running",
      }),
    ]);
    renderSidebar();

    const slot = screen.getByTestId("session-state-badge").parentElement!;
    // Fixed centered box so the dot centers on the same vertical line as the
    // rows' dots.
    expect(slot).toHaveClass("w-6", "justify-center");
    // Visible touch controls get their own column beside the marker.
    expect(slot).toHaveClass(
      "mr-7",
      "[@media((hover:hover)_and_(pointer:fine))]:mr-14",
      "[@media((hover:hover)_and_(pointer:fine))]:md:-mr-1",
    );
    expect(slot).toHaveClass(
      "[@media((hover:hover)_and_(pointer:fine))]:md:group-hover/section:opacity-0",
      "[@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-state=open]]/header:opacity-0",
      "[@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-header-controls]:focus-within]/header:opacity-0",
    );
  });

  // The "awaiting" pill is wider than the dot markers; constraining it to the
  // size-6 box would clip its "Needs response" label, so it keeps its natural
  // width.
  it("does not constrain the collapsed-project awaiting pill to the dot slot", () => {
    projectsMock.push("Customer X");
    mockConversations([
      conv("conv_awaiting", "Claude Code", {
        labels: { omni_project: "Customer X" },
        pending_elicitations_count: 1,
      }),
    ]);
    renderSidebar();

    const slot = screen.getByTestId("session-state-badge").parentElement!;
    expect(slot).not.toHaveClass("w-6");
  });
});

// Every section is expanded by default, but a collapse the user makes
// persists across reloads.
describe("Sidebar default section collapse", () => {
  it("expands Pinned and Sessions by default when there is no stored preference", () => {
    seedPins(["conv_pin"]);
    mockConversations([conv("conv_pin", "Claude Code"), conv("conv_recent", "Claude Code")]);
    renderSidebar();

    expect(screen.getByRole("button", { name: /Pinned/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Sessions/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("honors a persisted collapse of the Sessions list across remount", () => {
    // "Chats" is the persisted collapse key (kept stable across the label
    // rename); the header it collapses now reads "Sessions".
    localStorage.setItem("omnigent:collapsed-sidebar-sections", JSON.stringify(["Chats"]));
    mockConversations([conv("conv_recent", "Claude Code")]);
    renderSidebar();

    expect(screen.getByRole("button", { name: /Sessions/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText("conv_recent")).toBeNull();
  });
});

// Pinning a session while the Pinned section is collapsed auto-expands it, so
// the just-pinned chat can't silently hide inside the collapsed group.
describe("Sidebar auto-expand Pinned on pin", () => {
  it("expands a collapsed Pinned section when a session is newly pinned", () => {
    localStorage.setItem("omnigent:collapsed-sidebar-sections", JSON.stringify(["Pinned"]));
    // Start with one already-pinned session (so the Pinned section renders) and
    // one unpinned session to pin.
    seedPins(["conv_pinned"]);
    mockConversations([conv("conv_pinned", "Claude Code"), conv("conv_plain", "Claude Code")]);
    // A stable tree so the rerender keeps the same Sidebar instance — the
    // auto-expand effect compares against the PREVIOUS pinned set, which only
    // works if the component (and its ref) survive the pinned-set change.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = () => (
      <QueryClientProvider client={qc}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/"]}>
              <Sidebar open onClose={vi.fn()} />
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(tree());

    // Collapsed to start: the header reports collapsed and the pinned row hides.
    expect(screen.getByRole("button", { name: /Pinned/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );

    // A new session becomes pinned server-side (as if the toggle landed and the
    // pinned query refetched); re-render the same tree so the effect sees the
    // transition and auto-expands.
    seedPins(["conv_pinned", "conv_plain"]);
    rerender(tree());

    // The Pinned section auto-expands so the freshly-pinned session is visible,
    // and the expansion is persisted (dropped from the collapsed list).
    expect(screen.getByRole("button", { name: /Pinned/ })).toHaveAttribute("aria-expanded", "true");
    expect(JSON.parse(localStorage.getItem("omnigent:collapsed-sidebar-sections")!)).not.toContain(
      "Pinned",
    );
  });
});

// The quick-pin affordance is hover-revealed on every row — including pinned
// ones. A pinned row no longer keeps a persistent pin marker (the "Pinned"
// section header already conveys the state); on hover it reveals the UNPIN
// control.
describe("Sidebar pin marker visibility", () => {
  it("hover-reveals an unpin control on a pinned row (no persistent marker)", () => {
    mockConversations([conv("conv_pin", "Claude Code")]);
    seedPins(["conv_pin"]);
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

  it("removes a session from its project immediately, with no confirmation", async () => {
    // A first-class project persists when emptied, so removing a session never
    // deletes anything — it just unfiles, even if it's the last member. No
    // last-session check, no confirmation dialog.
    projectsMock.push("Sprint 42");
    mockConversations([
      conv("conv_filed", "Claude Code", { labels: { omni_project: "Sprint 42" } }),
    ]);
    renderSidebar();

    // Expand the project folder, open the filed row's kebab → project submenu.
    fireEvent.click(screen.getByRole("button", { name: "Sprint 42" }));
    const row = screen.getByRole("link", { name: /conv_filed/ }).closest("li")!;
    fireEvent.pointerDown(within(row).getByRole("button", { name: "Conversation actions" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByTestId("move-to-project"));

    // "Remove from <project>" unfiles immediately (project: "") — no dialog.
    fireEvent.click(await screen.findByRole("menuitem", { name: /Remove from Sprint 42/ }));
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
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={[initialEntry]}>
              <Routes>
                <Route path="/" element={<Sidebar open onClose={vi.fn()} />} />
                <Route path="/c/:conversationId" element={<Sidebar open onClose={vi.fn()} />} />
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
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

  it("hides the draft indicator for the active conversation", () => {
    vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
    mockConversations([conv("conv_active", "Claude Code", { title: "Active draft" })]);
    setSessionDraft("conv_active", { text: "visible in the open composer", files: [] });

    renderAtRoute("/c/conv_active");

    const row = screen.getByText("Active draft").closest("li")!;
    expect(within(row).queryByTestId("conversation-draft-indicator")).toBeNull();
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

it("caps Shared display independently of Mine while revealing cached rows", async () => {
  isServerLocalMock.mockReturnValue(false);
  const owned = Array.from({ length: 40 }, (_, i) => conv(`owned-${i}`, "agent"));
  const shared = Array.from({ length: 70 }, (_, i) =>
    conv(`shared-${i}`, "agent", {
      owner: "other@example.com",
      permission_level: 1,
      updated_at: 100 - i,
    }),
  );
  mockConversations([...owned, ...shared]);
  renderSidebar(true, "/", undefined, undefined, [], vi.fn(), {
    ...sidebarConfig,
    sharedDisplayPageSize: 30,
  });
  selectSessionFilter("mine");
  expect(screen.getByText("owned-39", { exact: true })).toBeInTheDocument();
  showSharedTab();
  expect(screen.getByText("shared-29", { exact: true })).toBeInTheDocument();
  expect(screen.queryByText("shared-30", { exact: true })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  await waitFor(() => expect(screen.getByText("shared-59", { exact: true })).toBeInTheDocument());
  expect(screen.queryByText("shared-60", { exact: true })).toBeNull();
  selectSessionFilter("mine");
  expect(screen.getByText("owned-39", { exact: true })).toBeInTheDocument();
  showSharedTab();
  expect(screen.queryByText("shared-30", { exact: true })).toBeNull();
});
