import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

import { CommandPalette } from "./CommandPalette";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
}));

const useConversations = vi.fn();
const useProjects = vi.fn();
const useSessionSearch = vi.fn();
const serverInfo = vi.fn();
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => serverInfo(),
}));
vi.mock("@/hooks/useConversations", () => ({
  useConversations: (...args: unknown[]) => useConversations(...args),
  useProjects: () => useProjects(),
  useSessionSearch: (...args: unknown[]) => useSessionSearch(...args),
}));

function conv(
  id: string,
  title: string | null,
  agent_name: string | null = null,
  search_snippet: string | null = null,
) {
  return { id, title, agent_name, archived: false, search_snippet };
}

function setSessions(sessions: ReturnType<typeof conv>[], isFetching = false) {
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

function searchPages(...pages: ReturnType<typeof conv>[][]) {
  return { data: { pages: pages.map((data) => ({ data })) } };
}

const SEARCH_GROUP_OPTIONS = { enabled: true, limit: 8 };

/** Find a session row by its full label text even when the highlighter has
    split it around a <mark> (so the text lives across several nodes). */
function labelRow(text: string) {
  return screen.getByText((_content, el) => el?.tagName === "SPAN" && el.textContent === text);
}

function renderPalette(overrides: Partial<ComponentProps<typeof CommandPalette>> = {}) {
  const props = {
    open: true,
    onOpenChange: vi.fn(),
    onToggleLeftSidebar: vi.fn(),
    onToggleRightSidebar: vi.fn(),
    ...overrides,
  };
  render(<CommandPalette {...props} />);
  return props;
}

beforeEach(() => {
  navigate.mockClear();
  useConversations.mockReset();
  useSessionSearch.mockReset();
  serverInfo.mockReturnValue({ session_search_scope: true });
  setSessions([]);
  useProjects.mockReturnValue({ data: [] });
  setSearchGroups({});
});
afterEach(cleanup);

describe("CommandPalette — sessions", () => {
  it.each([false, true])(
    "requests active sessions and keeps owned and shared results (sessionsOnly=%s)",
    (sessionsOnly) => {
      const sessions = [
        { ...conv("owned", "Owned session"), owner: "current-user", permission_level: 4 },
        { ...conv("shared", "Shared session"), owner: "other-user", permission_level: 2 },
        { ...conv("archived", "Archived session"), archived: true },
      ];
      setSessions(sessions);
      renderPalette({ sessionsOnly });

      expect(useConversations).toHaveBeenCalledWith("", false, { enabled: true });
      expect(screen.getByText("Owned session")).toBeTruthy();
      expect(screen.getByText("Shared session")).toBeTruthy();
      expect(screen.queryByText("Archived session")).toBeNull();
    },
  );

  it("fuzzy-matches session names without showing unrelated actions", () => {
    setSessions([
      conv("parser", "Fix the parser"),
      conv("deploy", "Deploy API"),
      conv("content", "Unrelated", null, "Fix the parser"),
    ]);
    const { onOpenChange } = renderPalette({ sessionsOnly: true });
    fireEvent.change(screen.getByTestId("command-palette-input"), { target: { value: "fxprs" } });
    expect(screen.getByText("Fix the parser")).toBeTruthy();
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
    const { rerender } = render(<CommandPalette {...props} />);
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
    expect(screen.getByRole("status").textContent).toContain("Couldn't load");
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
      expect(screen.getByRole("status").textContent).toContain("Couldn't load sessions.");
      expect(screen.queryByText("No results found")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      expect(refetch).toHaveBeenCalledOnce();
    },
  );

  it("shows only one loading message during the initial session fetch", () => {
    setSessions([], true);
    renderPalette({ sessionsOnly: true });
    expect(screen.getByRole("status").textContent).toBe("Loading sessions…");
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

  it("bounds rendered rows while searching every loaded session", () => {
    setSessions(Array.from({ length: 100 }, (_, i) => conv(`c${i}`, `Session ${i}`)));
    renderPalette({ sessionsOnly: true });
    expect(screen.getAllByRole("option")).toHaveLength(50);
    expect(screen.getByRole("status").textContent).toContain("Showing 50 of 100 matches.");
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

      expect(useConversations).toHaveBeenCalledWith("", false, { enabled: true });

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

  it("indents session rows so their label aligns with the icon-prefixed actions", () => {
    setSessions([conv("c1", "Fix the parser")]);
    renderPalette();

    // Session items carry no leading icon, so they're padded to line up with
    // the Action rows' icon + gap. Assert the class so the alignment can't
    // silently regress.
    const item = screen.getByText("Fix the parser").closest("[data-slot=command-item]");
    expect(item?.className).toContain("pl-6");
  });
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

describe("CommandPalette — new chat in project", () => {
  beforeEach(() => {
    useProjects.mockReturnValue({
      data: [
        { id: "p1", name: "omnigent", icon: "🔥" },
        { id: null, name: "home lab", icon: null },
      ],
    });
  });

  it("hides project rows until the user types", () => {
    renderPalette();
    expect(screen.queryByText("New chat in project")).toBeNull();
    expect(screen.queryByText("New chat in omnigent")).toBeNull();
  });

  it("matches every typed word against the row label", () => {
    renderPalette();
    fireEvent.change(screen.getByTestId("command-palette-input"), {
      target: { value: "new omni" },
    });
    expect(screen.getByText("New chat in omnigent")).toBeTruthy();
    expect(screen.queryByText("New chat in home lab")).toBeNull();
    expect(screen.getByTestId("project-icon").textContent).toBe("🔥");
  });

  it("lists every project when the query is the group heading's word", () => {
    renderPalette();
    fireEvent.change(screen.getByTestId("command-palette-input"), {
      target: { value: "project" },
    });
    expect(screen.getByText("New chat in omnigent")).toBeTruthy();
    expect(screen.getByText("New chat in home lab")).toBeTruthy();
  });

  it("opens the highlighted project row with Enter", () => {
    const { onOpenChange } = renderPalette();
    const input = screen.getByTestId("command-palette-input");
    fireEvent.change(input, { target: { value: "omni" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/?project=omnigent");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("opens the composer with the project's defaults and closes", () => {
    const { onOpenChange } = renderPalette();
    fireEvent.change(screen.getByTestId("command-palette-input"), {
      target: { value: "home" },
    });
    fireEvent.click(screen.getByText("New chat in home lab"));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(navigate).toHaveBeenCalledWith("/?project=home%20lab");
  });

  it("routes the New chat action through onNewSession", () => {
    const onNewSession = vi.fn();
    const { onOpenChange } = renderPalette({ onNewSession });
    fireEvent.click(screen.getByText("New chat"));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(onNewSession).toHaveBeenCalledTimes(1);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("omits project rows in the sessions-only switcher", () => {
    renderPalette({ sessionsOnly: true });
    fireEvent.change(screen.getByTestId("command-palette-input"), {
      target: { value: "omni" },
    });
    expect(screen.queryByText("New chat in omnigent")).toBeNull();
  });
});

describe("CommandPalette — project step", () => {
  beforeEach(() => {
    setSessions([conv("s1", "Cached session")]);
    useProjects.mockReturnValue({
      data: [
        { id: "p1", name: "omnigent", icon: "🔥" },
        { id: null, name: "home lab", icon: null },
      ],
    });
  });

  it("offers No project first, then every project, and nothing else", () => {
    renderPalette({ projectStep: true });

    expect(screen.getByPlaceholderText("Start a new session in…")).toBeTruthy();
    const rows = screen.getAllByRole("option").map((row) => row.textContent);
    expect(rows).toEqual(["No project", "🔥omnigent", "home lab"]);
    expect(useConversations).toHaveBeenCalledWith("", false, { enabled: false });
  });

  it("filters projects by name as the user types", () => {
    renderPalette({ projectStep: true });
    fireEvent.change(screen.getByTestId("command-palette-input"), {
      target: { value: "lab" },
    });
    expect(screen.getAllByRole("option").map((row) => row.textContent)).toEqual(["home lab"]);
  });

  it.each([
    ["No project", "/"],
    ["home lab", "/?project=home%20lab"],
  ])("opens the composer for %s", (row, path) => {
    const { onOpenChange } = renderPalette({ projectStep: true });
    fireEvent.click(screen.getByText(row));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(navigate).toHaveBeenCalledWith(path);
  });

  it.each([
    ["", "/"],
    ["lab", "/?project=home%20lab"],
  ])("opens the first row with Enter after typing %j", (typed, path) => {
    const { onOpenChange } = renderPalette({ projectStep: true });
    const input = screen.getByTestId("command-palette-input");
    if (typed) fireEvent.change(input, { target: { value: typed } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith(path);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("hides a stale session-load error", () => {
    useConversations.mockReturnValue({ isError: true, isFetching: false, refetch: vi.fn() });
    renderPalette({ projectStep: true });
    expect(screen.queryByText("Couldn't load sessions.")).toBeNull();
    expect(screen.getAllByRole("option").map((row) => row.textContent)).toEqual([
      "No project",
      "🔥omnigent",
      "home lab",
    ]);
  });
});

describe("CommandPalette — input", () => {
  it("uses the sessions-first placeholder", () => {
    renderPalette();

    expect(screen.getByPlaceholderText("Search sessions or run a command")).toBeTruthy();
  });
});

describe("CommandPalette — mobile full-screen sheet", () => {
  // useIsMobileViewport reads window.matchMedia("(max-width: 767.98px)").matches.
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

  it("does not search while the palette is closed or listing sessions only", () => {
    renderPalette({ open: false });
    expect(useSessionSearch).toHaveBeenCalledWith("", "title", { enabled: false, limit: 8 });
    cleanup();
    useSessionSearch.mockClear();
    renderPalette({ sessionsOnly: true });
    search("deploy");
    expect(useSessionSearch).toHaveBeenLastCalledWith("deploy", "content", {
      enabled: false,
      limit: 8,
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
    const { rerender } = render(<CommandPalette {...props} />);
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
    const { rerender } = render(<CommandPalette {...props} />);
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

  it("selects the top row again when the palette reopens", () => {
    setSessions([conv("c1", "Fix the parser"), conv("c2", "Deploy API")]);
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const { rerender } = render(<CommandPalette {...props} />);
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
    const { rerender } = render(<CommandPalette {...props} />);
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
    const { rerender } = render(<CommandPalette {...props} />);
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

  it("holds the search until server info loads, then sends only the grouped requests", () => {
    serverInfo.mockReturnValue("loading");
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const { rerender } = render(<CommandPalette {...props} />);
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
    const { rerender } = render(<CommandPalette {...props} />);
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

    expect(screen.getByRole("status").textContent).toContain("Couldn't load more sessions.");
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

    expect(screen.getByRole("status").textContent).toContain("Couldn't load sessions.");
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

    expect(screen.getByText("Go to Settings")).toBeTruthy();
    expect(screen.queryByText("New chat")).toBeNull();
  });
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
