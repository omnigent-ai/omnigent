import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import type * as SidebarDataModule from "@/hooks/useSidebarData";
import { type ComponentProps, type ReactNode, useState } from "react";
import { toast } from "sonner";

import { ALT_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import * as host from "@/lib/host";
import { resetUiFeatureOverridesForTests } from "@/lib/uiFeatureOverrides";
import * as commandPaletteModule from "./CommandPalette";
import { CommandPalette } from "./CommandPalette";
import type { WorkspaceHistoryFacts } from "./workspaceCommands/model";
import { useWorkspaceHistoryCommands } from "./workspaceCommands/useWorkspaceHistoryCommands";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
  useLocation: () => ({ pathname: "/", search: "", hash: "", state: null, key: "default" }),
}));

const useConversations = vi.fn();
const useSessionSearch = vi.fn();
const useProjects = vi.fn();
const serverInfo = vi.fn();
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => serverInfo(),
}));
vi.mock("@/hooks/useConversations", () => ({
  useConversations: (...args: unknown[]) => useConversations(...args),
  useSessionSearch: (...args: unknown[]) => useSessionSearch(...args),
  useProjects: (...args: unknown[]) => useProjects(...args),
}));

let sidebarSessions: { pages: { data: unknown[] }[] } | undefined;
vi.mock("@/hooks/useSidebarData", async (importOriginal) => ({
  ...(await importOriginal<typeof SidebarDataModule>()),
  useOptionalLoadedConversations: () => sidebarSessions,
}));

function conv(
  id: string,
  title: string | null,
  agent_name: string | null = null,
  search_snippet: string | null = null,
) {
  return { id, title, agent_name, archived: false, search_snippet };
}

function setSessions(
  sessions: (ReturnType<typeof conv> & Partial<Conversation>)[],
  isFetching = false,
) {
  useConversations.mockReturnValue({ data: { pages: [{ data: sessions }] }, isFetching });
}

type SearchGroupState = Record<string, unknown>;

/** Serve each typed-search group (`title` / `content` / `all`) its own hook state. */
function setSearchGroups({
  title = {},
  content = {},
  all = {},
}: {
  title?: SearchGroupState;
  content?: SearchGroupState;
  all?: SearchGroupState;
}) {
  const groups: Record<string, SearchGroupState> = { title, content, all };
  useSessionSearch.mockImplementation((_query: string, scope: string) => ({
    isFetching: false,
    ...groups[scope],
  }));
}

function searchPages(...pages: (ReturnType<typeof conv> & Partial<Conversation>)[][]) {
  return { data: { pages: pages.map((data) => ({ data })) } };
}

// ⌘K's own search keeps archived sessions, as the server does by default.
const SEARCH_GROUP_OPTIONS = { enabled: true, limit: 8, includeArchived: true };

/** Find a session row by its full label text even when the highlighter has
    split it around a <mark> (so the text lives across several nodes). */
function labelRow(text: string) {
  return screen.getByText((_content, el) => el?.tagName === "SPAN" && el.textContent === text);
}

function renderPalette(
  overrides: Partial<ComponentProps<typeof CommandPalette>> = {},
  queryClient = new QueryClient(),
) {
  const props = {
    open: true,
    onOpenChange: vi.fn(),
    onToggleLeftSidebar: vi.fn(),
    onToggleRightSidebar: vi.fn(),
    ...overrides,
  };
  render(<CommandPalette {...props} />, { wrapper: withQueryClient(queryClient) });
  return props;
}

function withQueryClient(queryClient = new QueryClient()) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  navigate.mockClear();
  sidebarSessions = undefined;
  useConversations.mockReset();
  useSessionSearch.mockReset();
  useProjects.mockReturnValue({ data: [] });
  serverInfo.mockReturnValue({ session_search_scope: true });
  setSessions([]);
  setSearchGroups({});
});
afterEach(() => {
  cleanup();
  // QuickPick records accepted commands under Recent; a later case starts without them.
  localStorage.clear();
  resetUiFeatureOverridesForTests();
});

/** Every non-empty live region's text, so both palettes' status messages read the same. */
const announced = () =>
  screen
    .getAllByRole("status")
    .map((region) => region.textContent)
    .filter(Boolean)
    .join(" ");

describe("CommandPalette — sessions", () => {
  it.each([false, true])(
    "keeps owned and shared sessions, and archived ones only in ⌘K (sessionsOnly=%s)",
    (sessionsOnly) => {
      const sessions = [
        { ...conv("owned", "Owned session"), owner: "current-user", permission_level: 4 },
        { ...conv("shared", "Shared session"), owner: "other-user", permission_level: 2 },
        { ...conv("archived", "Archived session"), archived: true },
      ];
      setSessions(sessions);
      renderPalette({ sessionsOnly });

      expect(useConversations).toHaveBeenCalledWith("", !sessionsOnly, { enabled: true });
      expect(screen.getByText("Owned session")).toBeTruthy();
      expect(screen.getByText("Shared session")).toBeTruthy();
      if (sessionsOnly) {
        expect(screen.queryByText("Archived session")).toBeNull();
      } else {
        expect(labelRow("Archived session").closest('[role="option"]')?.textContent).toContain(
          "Archived",
        );
      }
    },
  );

  it("lists a just-archived session first among the recent sessions and opens it", () => {
    // Archiving bumps updated_at, so the session lands above older activity.
    setSessions([
      ...Array.from({ length: 5 }, (_, index) => ({
        ...conv(`active-${index}`, `Active ${index}`),
        updated_at: 10 - index,
      })),
      { ...conv("mistake", "Archived too early"), archived: true, updated_at: 20 },
    ]);
    const { onOpenChange } = renderPalette();

    const rows = screen
      .getAllByRole("option")
      .map((option) => option.textContent ?? "")
      .filter((text) => text.includes("Active") || text.includes("Archived too early"));
    expect(rows).toHaveLength(5);
    expect(rows[0]).toContain("Archived too early");
    expect(rows[0]).toContain("Archived");
    expect(rows.slice(1).some((text) => text.includes("Archived"))).toBe(false);
    expect(screen.queryByText("Active 4")).toBeNull();

    fireEvent.keyDown(screen.getByTestId("command-palette-input"), { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/mistake");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("shows the sidebar's loaded sessions until the archived-inclusive list loads", () => {
    sidebarSessions = { pages: [{ data: [conv("cached", "Cached session")] }] };
    useConversations.mockReturnValue({ data: undefined, isFetching: true });
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const { rerender } = render(<CommandPalette {...props} />, { wrapper: withQueryClient() });

    expect(screen.getByText("Cached session")).toBeTruthy();

    // Once loaded, the list replaces the sidebar's rows, even when it's empty.
    setSessions([]);
    rerender(<CommandPalette {...props} />);
    expect(screen.queryByText("Cached session")).toBeNull();
  });

  it("doesn't fill Find a session's list from the sidebar", () => {
    sidebarSessions = { pages: [{ data: [conv("cached", "Cached session")] }] };
    useConversations.mockReturnValue({ data: undefined, isFetching: true });
    renderPalette({ sessionsOnly: true });

    expect(screen.queryByText("Cached session")).toBeNull();
  });

  it("keeps the row of a session opened from the recent list for the sidebar to list", () => {
    const archived = { ...conv("archived", "Archived session"), archived: true, owner: "me" };
    setSessions([archived]);
    const queryClient = new QueryClient();
    renderPalette({}, queryClient);

    fireEvent.click(labelRow("Archived session"));

    expect(navigate).toHaveBeenCalledWith("/c/archived");
    expect(queryClient.getQueryData(["conversation-backfill", "archived"])).toEqual(archived);
  });

  it.each([false, true])(
    "shows project and parent context without needing the parent in results (sessionsOnly=%s)",
    (sessionsOnly) => {
      useProjects.mockReturnValue({ data: [{ id: "project", name: "App redesign" }] });
      setSessions([
        {
          ...conv("nested", "Implement search"),
          project_id: "project",
          labels: {},
          family: {
            id: "family",
            representative_id: "parent",
            representative_title: "Search experience",
            pinned: false,
          },
        },
        { ...conv("standalone", "Standalone task"), labels: {} },
      ]);
      const { onOpenChange } = renderPalette({ sessionsOnly });
      expect(
        screen.getByRole("option", {
          name: /Implement search.*Project:\s*App redesign.*Nested under:\s*Search experience/,
        }),
      ).toBeTruthy();
      expect(
        labelRow("Standalone task").closest('[role="option"]')!.querySelector("[title]"),
      ).toBeNull();
      fireEvent.click(labelRow("Implement search"));
      expect(navigate).toHaveBeenCalledWith("/c/nested");
      expect(onOpenChange).toHaveBeenCalledWith(false);
    },
  );

  it.each(["recent", "title", "content"])(
    "uses a parent loaded in %s against older servers without title metadata",
    (parentSource) => {
      const parent = conv("parent", "Planning");
      const nested = {
        ...conv("nested", "Follow-up"),
        family: { id: "family", representative_id: "parent", pinned: false },
      };
      if (parentSource === "recent") {
        setSessions([parent, nested]);
      } else {
        setSearchGroups({
          title: searchPages(parentSource === "title" ? [parent] : [nested]),
          content: searchPages(parentSource === "content" ? [parent] : [nested]),
        });
      }
      renderPalette();
      if (parentSource !== "recent") {
        vi.useFakeTimers();
        try {
          fireEvent.change(screen.getByTestId("command-palette-input"), {
            target: { value: "Follow-up" },
          });
          act(() => vi.advanceTimersByTime(300));
        } finally {
          vi.useRealTimers();
        }
      }
      expect(
        screen.getByRole("option", { name: /Follow-up.*Nested under:\s*Planning/ }),
      ).toBeTruthy();
    },
  );

  it("uses legacy project labels and omits the parent breadcrumb on the representative", () => {
    setSessions([
      {
        ...conv("parent", "Planning"),
        labels: { omni_project: "Legacy project" },
        family: {
          id: "family",
          representative_id: "parent",
          representative_title: "Planning",
          pinned: false,
        },
      },
    ]);
    renderPalette();
    expect(
      screen.getByRole("option", { name: /Planning.*Project:\s*Legacy project/ }),
    ).toBeTruthy();
    expect(screen.queryByText(/Nested under/)).toBeNull();
  });

  it("fuzzy-matches session names without showing unrelated actions", () => {
    setSessions([
      conv("parser", "Fix the parser"),
      conv("deploy", "Deploy API"),
      conv("content", "Unrelated", null, "Fix the parser"),
    ]);
    const { onOpenChange } = renderPalette({ sessionsOnly: true });
    fireEvent.change(screen.getByTestId("command-palette-input"), { target: { value: "fxprs" } });
    // By name: a palette that marks each fuzzy-matched letter splits the label's text.
    expect(screen.getByRole("option", { name: /Fix the parser/ })).toBeTruthy();
    expect(screen.queryByText("Deploy API")).toBeNull();
    expect(screen.queryByText("Unrelated")).toBeNull();
    expect(screen.queryByText("Go to Inbox")).toBeNull();
    fireEvent.keyDown(screen.getByTestId("command-palette-input"), { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/parser");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("loads later pages for fuzzy matching and stops loading while closed", () => {
    const fetchNextPage = vi.fn();
    useConversations.mockReturnValue({
      data: { pages: [{ data: [] }] },
      isFetching: false,
      hasNextPage: true,
      fetchNextPage,
    });
    const props = {
      open: true,
      sessionsOnly: true,
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const { rerender } = render(<CommandPalette {...props} />, {
      wrapper: withQueryClient(),
    });
    expect(useConversations).toHaveBeenCalledWith("", false, { enabled: true });
    expect(fetchNextPage).toHaveBeenCalledOnce();
    useConversations.mockReturnValue({
      data: { pages: [{ data: [] }, { data: [conv("older", "Older session")] }] },
      isFetching: false,
      hasNextPage: false,
      fetchNextPage,
    });
    rerender(<CommandPalette {...props} />);
    expect(screen.getByText("Older session")).toBeTruthy();
    rerender(<CommandPalette {...props} open={false} />);
    expect(useConversations).toHaveBeenLastCalledWith("", false, { enabled: false });
    expect(fetchNextPage).toHaveBeenCalledOnce();
  });

  it("does not retry a failed pagination request indefinitely", () => {
    const fetchNextPage = vi.fn();
    useConversations.mockReturnValue({
      data: { pages: [] },
      isFetching: false,
      hasNextPage: true,
      isFetchNextPageError: true,
      fetchNextPage,
    });
    renderPalette({ sessionsOnly: true });
    expect(fetchNextPage).not.toHaveBeenCalled();
    expect(announced()).toContain("Couldn't load");
    expect(screen.queryByText("No results found")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(fetchNextPage).toHaveBeenCalledOnce();
  });

  it.each([false, true])(
    "shows initial fetch errors with a retry (sessionsOnly=%s)",
    (sessionsOnly) => {
      const refetch = vi.fn();
      useConversations.mockReturnValue({ isError: true, isFetching: false, refetch });
      renderPalette({ sessionsOnly });
      expect(announced()).toContain("Couldn't load sessions.");
      expect(screen.queryByText("No results found")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      expect(refetch).toHaveBeenCalledOnce();
    },
  );

  it("shows only one loading message during the initial session fetch", () => {
    setSessions([], true);
    renderPalette({ sessionsOnly: true });
    expect(announced()).toBe("Loading sessions…");
    expect(screen.queryByText("Searching…")).toBeNull();
    expect(screen.queryByText("No results found")).toBeNull();
  });

  it("bounds automatic pagination and lets the user search older sessions", () => {
    const fetchNextPage = vi.fn();
    useConversations.mockReturnValue({
      data: {
        pages: Array.from({ length: 10 }, () => ({ data: [conv("repeated", "Repeated page")] })),
      },
      hasNextPage: true,
      isFetching: false,
      fetchNextPage,
    });
    renderPalette({ sessionsOnly: true });
    expect(fetchNextPage).not.toHaveBeenCalled();
    expect(screen.getAllByText("Repeated page")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Search older sessions" }));
    expect(fetchNextPage).toHaveBeenCalledOnce();
  });

  it("keeps the match cap visible beside a load error", () => {
    useConversations.mockReturnValue({
      data: {
        pages: [{ data: Array.from({ length: 60 }, (_, i) => conv(`c${i}`, `Session ${i}`)) }],
      },
      isFetching: false,
      hasNextPage: true,
      isFetchNextPageError: true,
      fetchNextPage: vi.fn(),
    });
    renderPalette({ sessionsOnly: true });
    expect(screen.getByText("Couldn't load more sessions.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.getByText(/Showing 50 of 60 matches\./)).toBeTruthy();
  });

  it("keeps the match cap visible while sessions reload", () => {
    setSessions(
      Array.from({ length: 60 }, (_, i) => conv(`c${i}`, `Session ${i}`)),
      true,
    );
    renderPalette({ sessionsOnly: true });
    expect(screen.getByText("Loading sessions…")).toBeTruthy();
    expect(screen.getByText(/Showing 50 of 60 matches\./)).toBeTruthy();
  });

  it("bounds rendered rows while searching every loaded session", () => {
    setSessions(Array.from({ length: 100 }, (_, i) => conv(`c${i}`, `Session ${i}`)));
    renderPalette({ sessionsOnly: true });
    expect(screen.getAllByRole("option")).toHaveLength(50);
    expect(announced()).toContain("Showing 50 of 100 matches.");
    fireEvent.change(screen.getByTestId("command-palette-input"), { target: { value: "Session" } });
    expect(screen.getAllByRole("option")).toHaveLength(50);
    expect(document.querySelector("mark")?.textContent).toBe("Session");
    fireEvent.change(screen.getByTestId("command-palette-input"), {
      target: { value: "Session 99" },
    });
    expect(labelRow("Session 99")).toBeTruthy();
    fireEvent.keyDown(screen.getByTestId("command-palette-input"), { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/c99");
  });
  it("shows each action's chord beside its row, and hides the workspace chord on grid routes", () => {
    const chordsFor = (label: string) =>
      Array.from(
        screen
          .getByText(label)
          .closest('[role="option"]')!
          .querySelectorAll<HTMLElement>('[data-slot="kbd"]'),
      ).map((key) => key.textContent);
    renderPalette();
    expect(chordsFor("Go to Settings")).toEqual([MOD_KEY, ALT_KEY, ","]);
    expect(chordsFor("Toggle conversations sidebar")).toEqual([MOD_KEY, ALT_KEY, "["]);
    expect(chordsFor("Toggle workspace sidebar")).toEqual([MOD_KEY, ALT_KEY, "]"]);
    expect(chordsFor("Go to Inbox")).toEqual([]);
    cleanup();

    renderPalette({ rightSidebarShortcutEnabled: false });
    expect(chordsFor("Toggle workspace sidebar")).toEqual([]);
  });

  it("lists sessions by display label with their agent type", () => {
    setSessions([conv("c1", "Fix the parser", "research-agent"), conv("c2", null)]);
    renderPalette();

    expect(screen.getByText("Fix the parser")).toBeTruthy();
    expect(screen.getByText("research-agent")).toBeTruthy();
    // Null title → conversationDisplayLabel's "New session" fallback.
    expect(screen.getByText("New session")).toBeTruthy();
  });

  it("navigates to the session and closes when an item is selected", () => {
    setSessions([conv("c1", "Fix the parser")]);
    const onOpenChange = vi.fn();
    renderPalette({ onOpenChange });

    fireEvent.click(screen.getByText("Fix the parser"));

    expect(navigate).toHaveBeenCalledWith("/c/c1");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("debounces the typed query into a server search", () => {
    vi.useFakeTimers();
    try {
      setSessions([conv("c1", "Fix the parser")]);
      renderPalette();

      expect(useConversations).toHaveBeenCalledWith("", true, { enabled: true });

      fireEvent.change(screen.getByTestId("command-palette-input"), {
        target: { value: "deploy" },
      });
      // Before the debounce elapses the query has NOT yet reached the search.
      expect(useSessionSearch).not.toHaveBeenCalledWith("deploy", "title", SEARCH_GROUP_OPTIONS);

      act(() => {
        vi.advanceTimersByTime(300);
      });
      // The debounced query searches titles and content as separate groups.
      expect(useSessionSearch).toHaveBeenCalledWith("deploy", "title", SEARCH_GROUP_OPTIONS);
      expect(useSessionSearch).toHaveBeenCalledWith("deploy", "content", SEARCH_GROUP_OPTIONS);
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders the Sessions group above Actions", () => {
    setSessions([conv("c1", "Fix the parser")]);
    renderPalette();

    // Group order matters: the palette doubles as the sidebar's session-search
    // entry point, so Sessions must come before the static Actions.
    const headings = screen.getAllByText(/^(Sessions|Actions)$/).map((el) => el.textContent);
    expect(headings).toEqual(["Sessions", "Actions"]);
  });

  it("caps the session list to 5 while the query is empty, lifting it on type", () => {
    vi.useFakeTimers();
    try {
      const many = Array.from({ length: 8 }, (_, i) => conv(`c${i}`, `Session ${i}`, "agent"));
      setSessions(many);
      setSearchGroups({ title: searchPages(many) });
      renderPalette();

      // Empty query: only the first 5 recent sessions show, so the Actions
      // group below stays visible without scrolling.
      expect(screen.getByText("Session 0")).toBeTruthy();
      expect(screen.getByText("Session 4")).toBeTruthy();
      expect(screen.queryByText("Session 5")).toBeNull();

      // Typing lifts the cap — finding a specific session is now the point.
      fireEvent.change(screen.getByTestId("command-palette-input"), {
        target: { value: "session" },
      });
      act(() => {
        vi.advanceTimersByTime(300);
      });
      // The label is now split around the highlighted query term
      // (`<mark>Session</mark> 5`), so match on the row's combined text.
      expect(labelRow("Session 5")).toBeTruthy();
      expect(labelRow("Session 7")).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("lists recent sessions newest first after a pushed update reorders them", () => {
    // The updates stream patches updated_at in place without refetching, so
    // the cached page can hold a just-bumped session below older ones.
    useConversations.mockReturnValue({
      data: {
        pages: [
          {
            data: [
              { ...conv("older", "Older session"), updated_at: 1 },
              { ...conv("bumped", "Bumped session"), updated_at: 3 },
              { ...conv("middle", "Middle session"), updated_at: 2 },
            ],
          },
        ],
      },
      isFetching: false,
    });
    renderPalette();
    const rows = screen
      .getAllByRole("option")
      .map((option) => option.textContent ?? "")
      .filter((text) => text.includes(" session"));
    expect(rows.map((text) => text.split(" session")[0])).toEqual(["Bumped", "Middle", "Older"]);
  });

  it("dedupes sessions that appear on overlapping pages", () => {
    useConversations.mockReturnValue({
      data: {
        pages: [{ data: [conv("c1", "One")] }, { data: [conv("c1", "One"), conv("c2", "Two")] }],
      },
      isFetching: false,
    });
    renderPalette();

    expect(screen.getAllByText("One")).toHaveLength(1);
    expect(screen.getByText("Two")).toBeTruthy();
  });

  // The QuickPick palette aligns them with a blank icon slot; its own test covers that.
  it.skipIf("QUICK_PICK_PALETTE" in commandPaletteModule)(
    "indents session rows so their label aligns with the icon-prefixed actions",
    () => {
      setSessions([conv("c1", "Fix the parser")]);
      renderPalette();

      // Session items carry no leading icon, so they're padded to line up with
      // the Action rows' icon + gap. Assert the class so the alignment can't
      // silently regress.
      const item = screen.getByText("Fix the parser").closest("[data-slot=command-item]");
      expect(item?.className).toContain("pl-6");
    },
  );
});

describe("CommandPalette — match preview", () => {
  // Drive a debounced query through so the palette highlights against it.
  function search(term: string) {
    fireEvent.change(screen.getByTestId("command-palette-input"), { target: { value: term } });
    act(() => {
      vi.advanceTimersByTime(300);
    });
  }

  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("keeps parent and project context alongside archived content matches", () => {
    useProjects.mockReturnValue({ data: [{ id: "project", name: "Search project" }] });
    setSearchGroups({
      content: searchPages([
        {
          ...conv("content", "Investigation", "codex", "The matcher uses a shared index"),
          project_id: "project",
          archived: true,
          family: {
            id: "family",
            representative_id: "parent",
            representative_title: "Search planning",
            pinned: false,
          },
        },
      ]),
    });
    renderPalette();
    search("matcher");
    expect(
      screen.getByRole("option", {
        name: /Investigation.*Project:\s*Search project.*Nested under:\s*Search planning/,
      }),
    ).toBeTruthy();
    expect(screen.getByText("Archived")).toBeTruthy();
    expect(labelRow("The matcher uses a shared index")).toBeTruthy();
  });

  it("shows the content snippet as a second line when the match is in the body", () => {
    setSearchGroups({
      content: searchPages([conv("c1", "Hello", "cursor", "…can you fix the what if I switch…")]),
    });
    renderPalette();
    search("what");

    // The title stays as the primary line; the snippet shows where it matched.
    expect(screen.getByText("Hello")).toBeTruthy();
    expect(screen.getByText(/can you fix the/)).toBeTruthy();
  });

  it("highlights the query term in both the title and the snippet", () => {
    setSearchGroups({
      title: searchPages([conv("t1", "what model", "cursor")]),
      content: searchPages([conv("c1", "Hello", "cursor", "Hello what model are you using?")]),
    });
    renderPalette();
    search("what");

    // Every occurrence of the query renders inside a <mark> (title + snippet).
    const marks = document.querySelectorAll("mark");
    expect(marks.length).toBeGreaterThanOrEqual(2);
    for (const m of marks) expect(m.textContent?.toLowerCase()).toBe("what");
  });

  it("omits the snippet line for a title-only match (no search_snippet)", () => {
    setSearchGroups({ title: searchPages([conv("c1", "deploy runbook", "agent", null)]) });
    renderPalette();
    search("deploy");

    expect(screen.getByText(/deploy/)).toBeTruthy();
    // No second line: only the single title row is present.
    expect(screen.queryByText(/runbook.*\n/)).toBeNull();
  });
});

describe("CommandPalette — focus as it closes", () => {
  /** A palette ⌘K opens from a terminal's input, with no trigger button, as the app does. */
  function ShortcutOpenedPalette() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <textarea
          aria-label="Terminal input"
          onKeyDown={(event) => {
            if (event.metaKey && event.key === "k") setOpen(true);
          }}
        />
        <CommandPalette
          open={open}
          onOpenChange={setOpen}
          onToggleLeftSidebar={vi.fn()}
          onToggleRightSidebar={vi.fn()}
        />
      </>
    );
  }

  async function openFromTerminal() {
    render(<ShortcutOpenedPalette />, { wrapper: withQueryClient() });
    const terminal = screen.getByRole("textbox", { name: "Terminal input" });
    terminal.focus();
    fireEvent.keyDown(terminal, { key: "k", metaKey: true });
    const input = await screen.findByTestId("command-palette-input");
    await vi.waitFor(() => expect(input).toHaveFocus());
    return { terminal, input };
  }

  it("gives the keyboard back to the field it opened from on Escape", async () => {
    const { terminal, input } = await openFromTerminal();
    fireEvent.keyDown(input, { key: "Escape" });
    await vi.waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await vi.waitFor(() => expect(terminal).toHaveFocus());
  });

  it("gives it back after running a command that leaves focus alone", async () => {
    const { terminal } = await openFromTerminal();
    fireEvent.click(await screen.findByRole("option", { name: /Toggle conversations sidebar/ }));
    await vi.waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await vi.waitFor(() => expect(terminal).toHaveFocus());
  });
});

describe("CommandPalette — input", () => {
  it("uses the sessions-first placeholder", () => {
    renderPalette();

    expect(screen.getByPlaceholderText("Search sessions or run a command")).toBeTruthy();
  });
});

describe("CommandPalette — mobile full-screen sheet", () => {
  // useIsMobileViewport reads window.matchMedia("(max-width: 1180.98px)").matches.
  // The global test-setup stubs matchMedia to always report false (desktop);
  // flip it here so the palette renders its keyboard-safe mobile layout.
  function setMobile(isMobile: boolean) {
    window.matchMedia = ((query: string) => ({
      matches: isMobile,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
  }

  function dialogContent() {
    return document.querySelector("[data-slot=dialog-content]");
  }

  afterEach(() => setMobile(false));

  it("renders a top-anchored full-screen sheet with a close button on mobile", () => {
    setMobile(true);
    renderPalette();

    // The sheet drops the centered-card geometry (top-1/4 → top-0, rounded → none)
    // so it fills the keyboard-aware viewport instead of hiding behind the keyboard.
    const content = dialogContent();
    expect(content?.className).toContain("rounded-none");
    expect(content?.className).not.toContain("top-1/4");
    // …and offers an explicit close affordance (no ⌘K/Esc hint on touch).
    expect(screen.getByLabelText("Close search")).toBeTruthy();
  });

  it("pins the mobile sheet to the top of the visible viewport in the iOS app", () => {
    // The iOS app's dialogs center on the keyboard-aware viewport inline; the sheet must not.
    vi.stubGlobal("omnigentNative", { kind: "ios" });
    onTestFinished(() => {
      vi.unstubAllGlobals();
    });
    setMobile(true);
    renderPalette();

    const style = (dialogContent() as HTMLElement | null)?.style;
    expect(style?.top).toBe("0px");
    expect(style?.height).toBe("var(--omnigent-viewport-height, 100dvh)");
    expect(style?.maxHeight).toBe("var(--omnigent-viewport-height, 100dvh)");
  });

  it("keeps the centered dialog and shows no close button on desktop", () => {
    setMobile(false);
    renderPalette();

    expect(dialogContent()?.className).toContain("top-1/4");
    expect(screen.queryByLabelText("Close search")).toBeNull();
  });

  it("closes the palette when the mobile close button is tapped", () => {
    setMobile(true);
    const onOpenChange = vi.fn();
    renderPalette({ onOpenChange });

    fireEvent.click(screen.getByLabelText("Close search"));

    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("still searches from the mobile input", () => {
    vi.useFakeTimers();
    try {
      setMobile(true);
      setSessions([conv("c1", "Fix the parser")]);
      renderPalette();

      fireEvent.change(screen.getByTestId("command-palette-input"), {
        target: { value: "deploy" },
      });
      act(() => {
        vi.advanceTimersByTime(300);
      });
      expect(useSessionSearch).toHaveBeenCalledWith("deploy", "title", SEARCH_GROUP_OPTIONS);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("CommandPalette — title and content groups", () => {
  function search(term: string) {
    fireEvent.change(screen.getByTestId("command-palette-input"), { target: { value: term } });
    act(() => {
      vi.advanceTimersByTime(300);
    });
  }

  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("lists title matches under Sessions before content matches under In messages", () => {
    setSearchGroups({
      title: searchPages([conv("old", "Kestrel plan")]),
      content: searchPages([conv("new", "Weekly sync", null, "the kestrel checklist")]),
    });
    renderPalette();
    search("kestrel");

    const headings = screen
      .getAllByText(/^(Sessions|In messages|Actions)$/)
      .map((el) => el.textContent);
    expect(headings).toEqual(["Sessions", "In messages"]);
    const options = screen.getAllByRole("option").map((el) => el.textContent ?? "");
    expect(options[0]).toContain("Kestrel plan");
    expect(options[1]).toContain("Weekly sync");
  });

  it("lists archived matches in both groups with an Archived badge", () => {
    setSearchGroups({
      title: searchPages([
        conv("active", "Kestrel plan"),
        { ...conv("retro", "Kestrel retro"), archived: true },
      ]),
      content: searchPages([
        { ...conv("sync", "Weekly sync", null, "the kestrel checklist"), archived: true },
      ]),
    });
    const { onOpenChange } = renderPalette();
    search("kestrel");

    const options = screen.getAllByRole("option").map((el) => el.textContent ?? "");
    expect(options).toHaveLength(3);
    expect(options[0]).toContain("Kestrel plan");
    expect(options[0]).not.toContain("Archived");
    expect(options[1]).toContain("Kestrel retro");
    expect(options[1]).toContain("Archived");
    expect(options[2]).toContain("Weekly sync");
    expect(options[2]).toContain("Archived");

    fireEvent.click(labelRow("Kestrel retro"));
    expect(navigate).toHaveBeenCalledWith("/c/retro");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps the picked search row, with its owner, for the sidebar to list", () => {
    const sharedRetro = {
      ...conv("retro", "Kestrel retro"),
      archived: true,
      owner: "teammate",
      permission_level: 2,
    };
    setSearchGroups({ title: searchPages([sharedRetro]) });
    const queryClient = new QueryClient();
    renderPalette({}, queryClient);
    search("kestrel");

    fireEvent.click(labelRow("Kestrel retro"));

    expect(navigate).toHaveBeenCalledWith("/c/retro");
    expect(queryClient.getQueryData(["conversation-backfill", "retro"])).toEqual(sharedRetro);
  });

  it("does not search while the palette is closed or listing sessions only", () => {
    renderPalette({ open: false });
    expect(useSessionSearch).toHaveBeenCalledWith("", "title", {
      enabled: false,
      limit: 8,
      includeArchived: true,
    });
    cleanup();
    useSessionSearch.mockClear();
    renderPalette({ sessionsOnly: true });
    search("deploy");
    expect(useSessionSearch).toHaveBeenLastCalledWith("deploy", "content", {
      enabled: false,
      limit: 8,
      includeArchived: true,
    });
  });

  it("keeps the top row selected when title matches arrive after content matches", () => {
    setSearchGroups({
      title: { isFetching: true },
      content: searchPages([conv("content", "Notes", null, "kestrel list")]),
    });
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const { rerender } = render(<CommandPalette {...props} />, {
      wrapper: withQueryClient(),
    });
    search("kestrel");
    setSearchGroups({
      title: searchPages([conv("title", "Kestrel plan")]),
      content: searchPages([conv("content", "Notes", null, "kestrel list")]),
    });
    rerender(<CommandPalette {...props} />);

    fireEvent.keyDown(screen.getByTestId("command-palette-input"), { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/title");
  });

  it("keeps the user's selection when title matches arrive later", () => {
    setSearchGroups({
      title: { isFetching: true },
      content: searchPages([
        conv("first", "Notes", null, "kestrel list"),
        conv("second", "Retro", null, "kestrel again"),
      ]),
    });
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const { rerender } = render(<CommandPalette {...props} />, {
      wrapper: withQueryClient(),
    });
    search("kestrel");
    const input = screen.getByTestId("command-palette-input");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    setSearchGroups({
      title: searchPages([conv("title", "Kestrel plan")]),
      content: searchPages([
        conv("first", "Notes", null, "kestrel list"),
        conv("second", "Retro", null, "kestrel again"),
      ]),
    });
    rerender(<CommandPalette {...props} />);

    fireEvent.keyDown(input, { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/second");
  });

  it("steps the rows with Ctrl-N and Ctrl-P as the arrows do, leaving Ctrl+J alone", () => {
    setSessions([conv("c1", "Fix the parser"), conv("c2", "Deploy API"), conv("c3", "Ship it")]);
    render(
      <CommandPalette
        open
        onOpenChange={vi.fn()}
        onToggleLeftSidebar={vi.fn()}
        onToggleRightSidebar={vi.fn()}
      />,
      { wrapper: withQueryClient() },
    );
    const input = screen.getByTestId("command-palette-input");
    input.focus();
    const selected = () => screen.getByRole("option", { selected: true }).textContent;
    expect(selected()).toContain("Fix the parser");
    fireEvent.keyDown(input, { key: "n", code: "KeyN", ctrlKey: true });
    expect(selected()).toContain("Deploy API");
    // The arrows and Ctrl-N/Ctrl-P walk the same selection.
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(selected()).toContain("Fix the parser");
    fireEvent.keyDown(input, { key: "n", code: "KeyN", ctrlKey: true });
    fireEvent.keyDown(input, { key: "n", code: "KeyN", ctrlKey: true });
    fireEvent.keyDown(input, { key: "p", code: "KeyP", ctrlKey: true });
    fireEvent.keyDown(input, { key: "j", code: "KeyJ", ctrlKey: true });
    // Neither key types into the field or takes focus from it.
    expect(input).toHaveFocus();
    expect(input).toHaveValue("");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/c2");
  });

  it("selects the top row again when the palette reopens", () => {
    setSessions([conv("c1", "Fix the parser"), conv("c2", "Deploy API")]);
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const { rerender } = render(<CommandPalette {...props} />, {
      wrapper: withQueryClient(),
    });
    fireEvent.keyDown(screen.getByTestId("command-palette-input"), { key: "ArrowDown" });
    rerender(<CommandPalette {...props} open={false} />);
    rerender(<CommandPalette {...props} />);

    fireEvent.keyDown(screen.getByTestId("command-palette-input"), { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/c1");
  });

  it("ignores pointer movement off the rows when title matches arrive later", () => {
    setSearchGroups({
      title: { isFetching: true },
      content: searchPages([conv("content", "Notes", null, "kestrel list")]),
    });
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const { rerender } = render(<CommandPalette {...props} />, {
      wrapper: withQueryClient(),
    });
    search("kestrel");
    fireEvent.pointerMove(screen.getByText("In messages"));
    setSearchGroups({
      title: searchPages([conv("title", "Kestrel plan")]),
      content: searchPages([conv("content", "Notes", null, "kestrel list")]),
    });
    rerender(<CommandPalette {...props} />);

    fireEvent.keyDown(screen.getByTestId("command-palette-input"), { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/title");
  });

  it("continues from the group's last row once Show more loads the final page", () => {
    const fetchContentPage = vi.fn();
    const firstPage = [
      conv("c1", "Notes", null, "deploy on Friday"),
      conv("c2", "Sync", null, "deploy checklist"),
    ];
    setSearchGroups({
      content: { ...searchPages(firstPage), hasNextPage: true, fetchNextPage: fetchContentPage },
    });
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const { rerender } = render(<CommandPalette {...props} />, {
      wrapper: withQueryClient(),
    });
    search("deploy");
    fireEvent.click(screen.getByText("Show more"));
    expect(fetchContentPage).toHaveBeenCalledOnce();
    setSearchGroups({
      content: searchPages(firstPage, [conv("c3", "Retro", null, "deploy went fine")]),
    });
    rerender(<CommandPalette {...props} />);

    const input = screen.getByTestId("command-palette-input");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/c3");
  });

  it("offers no Show more for a group whose matches all appear under Sessions", () => {
    setSearchGroups({
      title: searchPages([conv("same", "Deploy plan")]),
      content: {
        ...searchPages([conv("same", "Deploy plan", null, "deploy on Friday")]),
        hasNextPage: true,
        fetchNextPage: vi.fn(),
      },
    });
    renderPalette();
    search("deploy");
    expect(screen.queryByText("In messages")).toBeNull();
    expect(screen.queryByText("Show more")).toBeNull();
    expect(screen.getAllByRole("option")).toHaveLength(1);
  });

  it("starts from the top row again after Show more and a reopen", () => {
    setSessions([conv("recent", "Recent session"), conv("parked", "Deploy plan")]);
    setSearchGroups({
      title: {
        ...searchPages([conv("parked", "Deploy plan")]),
        hasNextPage: true,
        fetchNextPage: vi.fn(),
      },
    });
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const { rerender } = render(<CommandPalette {...props} />, { wrapper: withQueryClient() });
    search("deploy");
    fireEvent.click(screen.getByText("Show more"));
    rerender(<CommandPalette {...props} open={false} />);
    act(() => {
      vi.advanceTimersByTime(300);
    });
    rerender(<CommandPalette {...props} />);
    fireEvent.keyDown(screen.getByTestId("command-palette-input"), { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/recent");
  });

  it("holds the search until server info loads, then sends only the grouped requests", () => {
    serverInfo.mockReturnValue("loading");
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const { rerender } = render(<CommandPalette {...props} />, {
      wrapper: withQueryClient(),
    });
    search("deploy");

    // Nothing is enabled yet, and the palette shows it is still searching.
    const enabledScopes = () =>
      useSessionSearch.mock.calls
        .filter(([query, , options]) => query === "deploy" && options.enabled)
        .map(([, scope]) => scope);
    expect(enabledScopes()).toEqual([]);
    expect(screen.getByText("Searching…")).toBeTruthy();
    expect(screen.queryByText("No results found")).toBeNull();

    serverInfo.mockReturnValue({ session_search_scope: true });
    rerender(<CommandPalette {...props} />);
    expect(new Set(enabledScopes())).toEqual(new Set(["title", "content"]));
  });

  it("falls back to the mixed search when server info stalls", () => {
    serverInfo.mockReturnValue("loading");
    setSearchGroups({ all: searchPages([conv("t1", "Deploy plan")]) });
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const { rerender } = render(<CommandPalette {...props} />, {
      wrapper: withQueryClient(),
    });
    search("deploy");
    const enabledScopes = () =>
      new Set(
        useSessionSearch.mock.calls
          .filter(([query, , options]) => query === "deploy" && options.enabled)
          .map(([, scope]) => scope),
      );
    act(() => {
      vi.advanceTimersByTime(1499);
    });
    expect(enabledScopes()).toEqual(new Set());

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(enabledScopes()).toEqual(new Set(["all"]));
    expect(labelRow("Deploy plan")).toBeTruthy();

    // A late answer still switches to the grouped requests.
    useSessionSearch.mockClear();
    serverInfo.mockReturnValue({ session_search_scope: true });
    rerender(<CommandPalette {...props} />);
    expect(enabledScopes()).toEqual(new Set(["title", "content"]));
  });

  it("lists one mixed group on an older server that ignores search_scope", () => {
    serverInfo.mockReturnValue({});
    setSearchGroups({
      all: searchPages([conv("t1", "Deploy plan"), conv("c1", "Notes", null, "deploy on Friday")]),
    });
    renderPalette();
    search("deploy");

    expect(useSessionSearch).toHaveBeenCalledWith("deploy", "all", SEARCH_GROUP_OPTIONS);
    expect(useSessionSearch).toHaveBeenCalledWith("deploy", "content", {
      enabled: false,
      limit: 8,
      includeArchived: true,
    });
    expect(screen.queryByText("In messages")).toBeNull();
    const options = screen.getAllByRole("option").map((el) => el.textContent ?? "");
    expect(options[0]).toContain("Deploy plan");
    expect(options[1]).toContain("deploy on Friday");
  });

  it("loads more of one group without paging the other", () => {
    const fetchTitlePage = vi.fn();
    const fetchContentPage = vi.fn();
    setSearchGroups({
      title: { ...searchPages([conv("t1", "Deploy plan")]), fetchNextPage: fetchTitlePage },
      content: {
        ...searchPages([conv("c1", "Notes", null, "deploy on Friday")]),
        hasNextPage: true,
        fetchNextPage: fetchContentPage,
      },
    });
    renderPalette();
    search("deploy");

    // Only the content group has more rows, so only it offers "Show more".
    const showMore = screen.getAllByText("Show more");
    expect(showMore).toHaveLength(1);
    fireEvent.click(showMore[0]);
    expect(fetchContentPage).toHaveBeenCalledOnce();
    expect(fetchTitlePage).not.toHaveBeenCalled();
  });

  it("lists a session in only one group when both requests return it", () => {
    setSearchGroups({
      title: searchPages([conv("s1", "Deploy plan")]),
      content: searchPages([conv("s1", "Deploy plan", null, "deploy now"), conv("s2", "Notes")]),
    });
    renderPalette();
    search("deploy");

    expect(
      screen.getAllByRole("option").filter((el) => el.textContent?.includes("Deploy plan")),
    ).toHaveLength(1);
    expect(screen.getByText("Notes")).toBeTruthy();
  });

  it("retries loading more for only the group whose next page failed", () => {
    const fetchTitlePage = vi.fn();
    const fetchContentPage = vi.fn();
    setSearchGroups({
      title: {
        ...searchPages([conv("t1", "Deploy plan")]),
        hasNextPage: true,
        isError: true,
        isFetchNextPageError: true,
        fetchNextPage: fetchTitlePage,
      },
      content: { ...searchPages([]), isFetching: true, fetchNextPage: fetchContentPage },
    });
    renderPalette();
    search("deploy");

    expect(announced()).toContain("Couldn't load more sessions.");
    // A slow content request doesn't block retrying the failed title page.
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(fetchTitlePage).toHaveBeenCalledOnce();
    expect(fetchContentPage).not.toHaveBeenCalled();
  });

  it("retries only the group whose search failed", () => {
    const refetchTitle = vi.fn();
    const refetchContent = vi.fn();
    setSearchGroups({
      title: { ...searchPages([conv("t1", "Deploy plan")]), refetch: refetchTitle },
      content: { isError: true, refetch: refetchContent },
    });
    renderPalette();
    search("deploy");

    expect(announced()).toContain("Couldn't load sessions.");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetchContent).toHaveBeenCalledOnce();
    expect(refetchTitle).not.toHaveBeenCalled();
  });
});

describe("CommandPalette — actions", () => {
  it("lists the built-in action commands", () => {
    renderPalette();

    expect(screen.getByText("New chat")).toBeTruthy();
    expect(screen.getByText("Go to Inbox")).toBeTruthy();
    expect(screen.getByText("Go to Settings")).toBeTruthy();
    expect(screen.getByText("Toggle conversations sidebar")).toBeTruthy();
    expect(screen.getByText("Toggle workspace sidebar")).toBeTruthy();
  });

  it("runs a navigation action and closes the palette", () => {
    const onOpenChange = vi.fn();
    renderPalette({ onOpenChange });

    fireEvent.click(screen.getByText("Go to Settings"));

    expect(navigate).toHaveBeenCalledWith("/settings");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("invokes the sidebar-toggle callbacks", () => {
    const onToggleLeftSidebar = vi.fn();
    const onToggleRightSidebar = vi.fn();
    renderPalette({ onToggleLeftSidebar, onToggleRightSidebar });

    fireEvent.click(screen.getByText("Toggle conversations sidebar"));
    expect(onToggleLeftSidebar).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText("Toggle workspace sidebar"));
    expect(onToggleRightSidebar).toHaveBeenCalledTimes(1);
  });

  it("filters actions client-side against the query", () => {
    renderPalette();

    fireEvent.change(screen.getByTestId("command-palette-input"), {
      target: { value: "settings" },
    });

    expect(screen.getByRole("option", { name: /Go to Settings/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /New chat/ })).toBeNull();
  });
});

describe("CommandPalette — QuickPick preview bootstrap", () => {
  const label = "Enable feature preview: new QuickPick command palette";
  const origin = window.location.origin;
  const key = `omnigent:ui-feature-overrides:v1:${JSON.stringify(origin)}`;
  const isQuickPick = "QUICK_PICK_PALETTE" in commandPaletteModule;

  function searchPreview() {
    fireEvent.change(screen.getByTestId("command-palette-input"), { target: { value: "preview" } });
  }

  const reload = vi.fn();
  beforeEach(() => {
    reload.mockReset();
    vi.stubGlobal("location", { origin, reload });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.skipIf(isQuickPick)(
    "offers the command only in search and saves the opt-in before reloading",
    () => {
      serverInfo.mockReturnValue({
        session_search_scope: true,
        features: { quick_pick_palette: false },
      });
      renderPalette();
      expect(screen.queryByRole("option", { name: label })).toBeNull();
      searchPreview();
      const command = screen.getByRole("option", { name: label });
      let savedAtReload: string | null = null;
      reload.mockImplementation(() => {
        savedAtReload = localStorage.getItem(key);
      });
      fireEvent.click(command);
      expect(localStorage.getItem(key)).toBe('{"quick_pick_palette":true}');
      expect(reload).toHaveBeenCalledOnce();
      expect(savedAtReload).toBe('{"quick_pick_palette":true}');
    },
  );

  it.skipIf(isQuickPick)("offers the command when an override turns QuickPick off", () => {
    localStorage.setItem(key, '{"quick_pick_palette":false}');
    serverInfo.mockReturnValue({
      session_search_scope: true,
      features: { quick_pick_palette: true },
    });
    renderPalette();
    searchPreview();
    expect(screen.getByRole("option", { name: label })).toBeVisible();
  });

  it.skipIf(isQuickPick)("does not reload if storage refuses the opt-in", () => {
    serverInfo.mockReturnValue({
      session_search_scope: true,
      features: { quick_pick_palette: false },
    });
    renderPalette();
    searchPreview();
    const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    const errorToast = vi.spyOn(toast, "error").mockImplementation(() => "");
    onTestFinished(() => {
      storage.mockRestore();
      errorToast.mockRestore();
    });
    fireEvent.click(screen.getByRole("option", { name: label }));
    expect(errorToast).toHaveBeenCalledWith("Couldn't save the feature preview on this device.");
    expect(reload).not.toHaveBeenCalled();
  });

  it.skipIf(isQuickPick).each(["server-on", "override-on", "unreported", "no-identity"])(
    "does not offer the bootstrap for %s",
    (scenario) => {
      if (scenario === "override-on") localStorage.setItem(key, '{"quick_pick_palette":true}');
      if (scenario === "no-identity") {
        const identity = vi.spyOn(host, "getOmnigentServerIdentity").mockReturnValue(null);
        onTestFinished(() => identity.mockRestore());
      }
      const features =
        scenario === "unreported" ? {} : { quick_pick_palette: scenario === "server-on" };
      serverInfo.mockReturnValue({ session_search_scope: true, features });
      renderPalette();
      searchPreview();
      expect(screen.queryByRole("option", { name: label })).toBeNull();
    },
  );
});

describe("CommandPalette — empty state", () => {
  it("shows an empty state when nothing matches", () => {
    setSessions([]);
    renderPalette();

    // A query that matches no action and no session.
    fireEvent.change(screen.getByTestId("command-palette-input"), {
      target: { value: "zzzznomatch" },
    });

    expect(screen.getByText("No results found")).toBeTruthy();
  });
});

describe("CommandPalette — commands a mounted feature offers", () => {
  const BACK_ONLY: WorkspaceHistoryFacts = {
    back: { target: "Files", waitingForPage: false },
    forward: { target: null, waitingForPage: false },
  };

  const FORWARD_ONLY: WorkspaceHistoryFacts = {
    back: { target: null, waitingForPage: false },
    forward: { target: "Files", waitingForPage: false },
  };

  /** The tabs sidebar's Back and Forward, registered as its host registers them. */
  function WorkspaceHistory({ go, facts }: { go: () => void; facts: WorkspaceHistoryFacts }) {
    useWorkspaceHistoryCommands({ conversationId: "conv_a", facts, go }, true);
    return null;
  }

  /** A palette opened from a button. */
  function OpenedPalette({
    go,
    facts = BACK_ONLY,
  }: {
    go: () => void;
    facts?: WorkspaceHistoryFacts;
  }) {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          Open palette
        </button>
        <button type="button">Where the command moves focus</button>
        <WorkspaceHistory go={go} facts={facts} />
        <CommandPalette
          open={open}
          onOpenChange={setOpen}
          onToggleLeftSidebar={vi.fn()}
          onToggleRightSidebar={vi.fn()}
        />
      </>
    );
  }

  const withSidebarFlag = (enabled: boolean) =>
    serverInfo.mockReturnValue({
      session_search_scope: true,
      features: { workspace_sidebar_v2: enabled },
    });

  function openPalette(go: () => void, facts?: WorkspaceHistoryFacts) {
    render(<OpenedPalette go={go} facts={facts} />, { wrapper: withQueryClient() });
    const opener = screen.getByRole("button", { name: "Open palette" });
    opener.focus();
    fireEvent.click(opener);
    return opener;
  }

  it("lists them with their chords, and one that can't run says why", async () => {
    withSidebarFlag(true);
    openPalette(vi.fn());
    fireEvent.change(screen.getByTestId("command-palette-input"), {
      target: { value: "in the workspace" },
    });

    const back = await screen.findByRole("option", { name: /Go back in the workspace/ });
    expect(back.textContent).toContain("←");
    const forward = screen.getByRole("option", { name: /Go forward in the workspace/ });
    expect(forward).toHaveAttribute("aria-disabled", "true");
    expect(forward.textContent).toContain("There's nothing to go forward to in the workspace.");
  });

  it("highlights the first one that can run, so Enter runs it past one that can't", async () => {
    withSidebarFlag(true);
    const go = vi.fn();
    openPalette(go, FORWARD_ONLY);
    const input = screen.getByTestId("command-palette-input");
    fireEvent.change(input, { target: { value: "in the workspace" } });

    const forward = await screen.findByRole("option", { name: /Go forward in the workspace/ });
    expect(screen.getByRole("option", { name: /Go back in the workspace/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    // Once the rows have settled, before anyone moves the selection.
    await new Promise((resolve) => {
      setTimeout(resolve, 300);
    });
    expect(forward).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "Enter" });
    await vi.waitFor(() => expect(go).toHaveBeenCalledWith("forward"));
  });

  it("runs one that moves focus once the palette has closed, so the focus stays", async () => {
    withSidebarFlag(true);
    let dialogAtRun: Element | null = null;
    const go = vi.fn(() => {
      dialogAtRun = screen.queryByRole("dialog");
      screen.getByText("Where the command moves focus").focus();
    });
    openPalette(go);

    fireEvent.click(await screen.findByRole("option", { name: /Go back in the workspace/ }));
    expect(go).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(go).toHaveBeenCalledOnce());
    expect(dialogAtRun).toBeNull();
    await new Promise((resolve) => {
      setTimeout(resolve, 10);
    });
    // The dialog's own focus return, to the opener, came first and didn't undo it.
    expect(document.activeElement).toBe(screen.getByText("Where the command moves focus"));
  });

  // The QuickPick palette lists them under its own flag; its own test covers that.
  it.skipIf("QUICK_PICK_PALETTE" in commandPaletteModule)(
    "lists none without the workspace_sidebar_v2 flag",
    async () => {
      withSidebarFlag(false);
      openPalette(vi.fn());
      fireEvent.change(screen.getByTestId("command-palette-input"), {
        target: { value: "in the workspace" },
      });

      await screen.findByText("No results found");
      expect(screen.queryByRole("option", { name: /Go back in the workspace/ })).toBeNull();
    },
  );
});

// Either palette's dialog content.
const PALETTE_CONTENT = "[data-command-palette], [data-quick-pick-dialog]";

/**
 * Gives the palette's content an enter animation while open and an exit one once closing, so
 * Radix keeps it mounted until the exit ends, as in the app.
 */
function stubExitAnimation() {
  let animationName = "enter";
  const computedStyle = window.getComputedStyle.bind(window);
  const styleSpy = vi.spyOn(window, "getComputedStyle").mockImplementation((element) => {
    const style = computedStyle(element);
    if (!(element instanceof HTMLElement) || !element.matches(PALETTE_CONTENT)) return style;
    return new Proxy(style, {
      get: (target, property) =>
        property === "animationName" ? animationName : Reflect.get(target, property),
    });
  });
  onTestFinished(() => styleSpy.mockRestore());
  return {
    exitNext: () => {
      animationName = "exit";
    },
    stop: () => {
      animationName = "none";
      styleSpy.mockRestore();
    },
  };
}

describe("CommandPalette reopened during its exit animation", () => {
  let setOpen: (open: boolean) => void = () => undefined;
  function Opener() {
    const [open, setOpenState] = useState(false);
    setOpen = setOpenState;
    return (
      <>
        <textarea aria-label="Editor" />
        <CommandPalette
          open={open}
          onOpenChange={setOpenState}
          onToggleLeftSidebar={vi.fn()}
          onToggleRightSidebar={vi.fn()}
        />
      </>
    );
  }

  it("focuses its search field again after a row click left focus on the list", async () => {
    const animation = stubExitAnimation();
    render(<Opener />, { wrapper: withQueryClient() });
    const editor = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Editor" });
    editor.focus();
    act(() => setOpen(true));
    await vi.waitFor(() => expect(screen.getByTestId("command-palette-input")).toHaveFocus());
    animation.exitNext();
    // Clicking a row focuses the list, and the row's command closes the palette.
    act(() => screen.getByRole("listbox").focus());
    act(() => setOpen(false));
    expect(document.querySelector(PALETTE_CONTENT)).not.toBeNull();

    // ⌘K again while it animates out.
    act(() => setOpen(true));
    const input = screen.getByTestId("command-palette-input");
    await vi.waitFor(() => expect(input).toHaveFocus());
    // The replaced content's late close doesn't pull focus back to the editor.
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: "settings" } });
    expect(input).toHaveValue("settings");

    // Cancelling still returns focus to where the user was before the first opening.
    animation.stop();
    fireEvent.keyDown(input, { key: "Escape" });
    await vi.waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // Radix returns focus just after the content unmounts.
    await vi.waitFor(() => expect(editor).toHaveFocus());
  });
});
