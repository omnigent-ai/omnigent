import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

import { CommandPalette } from "./CommandPalette";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
}));

const useConversations = vi.fn();
vi.mock("@/hooks/useConversations", () => ({
  useConversations: (...args: unknown[]) => useConversations(...args),
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

/** Find a session row by its full label text even when the highlighter has
    split it around a <mark> (so the text lives across several nodes). */
function labelRow(text: string) {
  return screen.getByText((_content, el) => el?.tagName === "SPAN" && el.textContent === text);
}

function renderPalette(overrides: Partial<ComponentProps<typeof CommandPalette>> = {}) {
  const props = {
    open: true,
    onOpenChange: vi.fn(),
    mode: "commands" as const,
    onToggleLeftSidebar: vi.fn(),
    onToggleRightSidebar: vi.fn(),
    ...overrides,
  };
  render(<CommandPalette {...props} />);
  return props;
}

/** Sessions mode — what ⌘⇧F opens. */
function renderSearch(overrides: Partial<ComponentProps<typeof CommandPalette>> = {}) {
  return renderPalette({ mode: "sessions", ...overrides });
}

beforeEach(() => {
  navigate.mockClear();
  useConversations.mockReset();
  setSessions([]);
});
afterEach(cleanup);

describe("CommandPalette — sessions mode", () => {
  it("lists sessions by display label with their agent type", () => {
    setSessions([conv("c1", "Fix the parser", "research-agent"), conv("c2", null)]);
    renderSearch();

    expect(screen.getByText("Fix the parser")).toBeTruthy();
    expect(screen.getByText("research-agent")).toBeTruthy();
    // Null title → conversationDisplayLabel's "New session" fallback.
    expect(screen.getByText("New session")).toBeTruthy();
  });

  it("navigates to the session and closes when an item is selected", () => {
    setSessions([conv("c1", "Fix the parser")]);
    const onOpenChange = vi.fn();
    renderSearch({ onOpenChange });

    fireEvent.click(screen.getByText("Fix the parser"));

    expect(navigate).toHaveBeenCalledWith("/c/c1");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("debounces the typed query into a server search (archived excluded)", () => {
    vi.useFakeTimers();
    try {
      setSessions([conv("c1", "Fix the parser")]);
      renderSearch();

      // Empty query on mount → shares AppShell's `["conversations","",true]` entry.
      expect(useConversations).toHaveBeenCalledWith("", true);

      fireEvent.change(screen.getByTestId("command-palette-input"), {
        target: { value: "deploy" },
      });
      // Before the debounce elapses the query has NOT yet reached the hook.
      expect(useConversations).not.toHaveBeenCalledWith("deploy", true);

      act(() => {
        vi.advanceTimersByTime(300);
      });
      // After the 300ms debounce, the typed query drives a server search with
      // archived rows included (filtered client-side) — proving search hits the
      // server, not a page.
      expect(useConversations).toHaveBeenCalledWith("deploy", true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("lists every match with no Actions group in the way", () => {
    const many = Array.from({ length: 8 }, (_, i) => conv(`c${i}`, `Session ${i}`, "agent"));
    setSessions(many);
    renderSearch();

    // Sessions mode is only sessions: no cap on the idle list (nothing below it
    // to keep above the fold) and no Actions group competing for the space.
    expect(screen.getByText("Session 0")).toBeTruthy();
    expect(screen.getByText("Session 7")).toBeTruthy();
    expect(screen.queryByText("Actions")).toBeNull();
    expect(screen.queryByText("New chat")).toBeNull();
  });

  it("still highlights matches after the query lands", () => {
    vi.useFakeTimers();
    try {
      setSessions([conv("c5", "Session 5", "agent")]);
      renderSearch();

      fireEvent.change(screen.getByTestId("command-palette-input"), {
        target: { value: "session" },
      });
      act(() => {
        vi.advanceTimersByTime(300);
      });
      // The label is now split around the highlighted query term
      // (`<mark>Session</mark> 5`), so match on the row's combined text.
      expect(labelRow("Session 5")).toBeTruthy();
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
    renderSearch();

    expect(screen.getAllByText("One")).toHaveLength(1);
    expect(screen.getByText("Two")).toBeTruthy();
  });

  it("uses the search placeholder", () => {
    renderSearch();

    expect(screen.getByPlaceholderText("Search sessions")).toBeTruthy();
  });

  it("shows a sessions-specific empty state", () => {
    setSessions([]);
    renderSearch();

    expect(screen.getByText("No sessions found")).toBeTruthy();
  });
});

describe("CommandPalette — match preview", () => {
  // Drive a debounced query through so the rows highlight against it.
  function search(term: string) {
    fireEvent.change(screen.getByTestId("command-palette-input"), { target: { value: term } });
    act(() => {
      vi.advanceTimersByTime(300);
    });
  }

  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("shows the content snippet as a second line when the match is in the body", () => {
    setSessions([conv("c1", "Hello", "cursor", "…can you fix the what if I switch…")]);
    renderSearch();
    search("what");

    // The title stays as the primary line; the snippet shows where it matched.
    expect(screen.getByText("Hello")).toBeTruthy();
    expect(screen.getByText(/can you fix the/)).toBeTruthy();
  });

  it("highlights the query term in both the title and the snippet", () => {
    setSessions([conv("c1", "what model", "cursor", "Hello what model are you using?")]);
    renderSearch();
    search("what");

    // Every occurrence of the query renders inside a <mark> (title + snippet).
    const marks = document.querySelectorAll("mark");
    expect(marks.length).toBeGreaterThanOrEqual(2);
    for (const m of marks) expect(m.textContent?.toLowerCase()).toBe("what");
  });

  it("omits the snippet line for a title-only match (no search_snippet)", () => {
    setSessions([conv("c1", "deploy runbook", "agent", null)]);
    renderSearch();
    search("deploy");

    expect(screen.getByText(/deploy/)).toBeTruthy();
    // No second line: only the single title row is present.
    expect(screen.queryByText(/runbook.*\n/)).toBeNull();
  });
});

describe("CommandPalette — input", () => {
  it("uses the commands placeholder in commands mode", () => {
    renderPalette();

    expect(screen.getByPlaceholderText("Run a command")).toBeTruthy();
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
      renderSearch();

      fireEvent.change(screen.getByTestId("command-palette-input"), {
        target: { value: "deploy" },
      });
      act(() => {
        vi.advanceTimersByTime(300);
      });
      expect(useConversations).toHaveBeenCalledWith("deploy", true);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("CommandPalette — commands mode", () => {
  it("lists the built-in action commands", () => {
    renderPalette();

    expect(screen.getByText("New chat")).toBeTruthy();
    expect(screen.getByText("Go to Inbox")).toBeTruthy();
    expect(screen.getByText("Go to Settings")).toBeTruthy();
    expect(screen.getByText("Toggle conversations sidebar")).toBeTruthy();
    expect(screen.getByText("Toggle workspace sidebar")).toBeTruthy();
  });

  it("never lists sessions, and never asks the server for any", () => {
    setSessions([conv("c1", "Fix the parser")]);
    renderPalette();

    // The whole point of the split: ⌘K is commands only, so the sessions half
    // isn't mounted and no session request is issued.
    expect(screen.queryByText("Fix the parser")).toBeNull();
    expect(screen.queryByText("Sessions")).toBeNull();
    expect(useConversations).not.toHaveBeenCalled();
  });

  it("uses the command placeholder", () => {
    renderPalette();

    expect(screen.getByPlaceholderText("Run a command")).toBeTruthy();
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

  it("shows a commands-specific empty state when nothing matches", () => {
    renderPalette();

    fireEvent.change(screen.getByTestId("command-palette-input"), {
      target: { value: "zzzznomatch" },
    });

    expect(screen.getByText("No commands found")).toBeTruthy();
  });
});
