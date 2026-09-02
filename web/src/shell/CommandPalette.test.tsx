import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState, type ComponentProps } from "react";
import { ActionsProvider, HANDLED, setUserKeybindingRule, useRegisterAction } from "@/actions";
import { resetKeybindingStoreForTesting } from "@/actions/KeybindingStore";
import { EmbeddedProvider } from "@/lib/embedded";

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

interface RenderPaletteOptions extends Partial<ComponentProps<typeof CommandPalette>> {
  onToggleLeftSidebar?: () => void;
  onToggleRightSidebar?: () => void;
  settingsEnabled?: boolean;
  inboxVisible?: boolean;
  onInvocation?: (source: string) => void;
  embedded?: boolean;
}

const NOOP = () => {};

function PaletteActions({
  onToggleLeftSidebar,
  onToggleRightSidebar,
  settingsEnabled = true,
  inboxVisible = true,
  onInvocation = NOOP,
}: {
  onToggleLeftSidebar: () => void;
  onToggleRightSidebar: () => void;
  settingsEnabled?: boolean;
  inboxVisible?: boolean;
  onInvocation?: (source: string) => void;
}) {
  const navigateAction = (path: string) => (invocation: { source: string }) => {
    onInvocation(invocation.source);
    navigate(path);
    return HANDLED;
  };
  useRegisterAction("session.action.new", { run: navigateAction("/") });
  useRegisterAction("workbench.action.showCommands", { run: () => HANDLED });
  useRegisterAction("workbench.action.navigateInbox", {
    run: navigateAction("/inbox"),
    isVisible: () => inboxVisible,
  });
  useRegisterAction("workbench.action.navigateAutomations", { run: navigateAction("/tasks") });
  useRegisterAction("workbench.action.navigateSettings", {
    run: navigateAction("/settings"),
    isEnabled: () => settingsEnabled,
  });
  useRegisterAction("workbench.action.toggleConversationsSidebar", {
    run: () => {
      onToggleLeftSidebar();
      return HANDLED;
    },
  });
  useRegisterAction("workbench.action.toggleWorkspaceSidebar", {
    run: () => {
      onToggleRightSidebar();
      return HANDLED;
    },
  });
  return null;
}

function renderPalette(overrides: RenderPaletteOptions = {}) {
  const props: ComponentProps<typeof CommandPalette> = {
    open: overrides.open ?? true,
    onOpenChange: overrides.onOpenChange ?? vi.fn(),
  };
  const onToggleLeftSidebar = overrides.onToggleLeftSidebar ?? vi.fn();
  const onToggleRightSidebar = overrides.onToggleRightSidebar ?? vi.fn();
  const content = (
    <ActionsProvider>
      <PaletteActions
        onToggleLeftSidebar={onToggleLeftSidebar}
        onToggleRightSidebar={onToggleRightSidebar}
        settingsEnabled={overrides.settingsEnabled}
        inboxVisible={overrides.inboxVisible}
        onInvocation={overrides.onInvocation}
      />
      <CommandPalette {...props} />
    </ActionsProvider>
  );
  render(overrides.embedded ? <EmbeddedProvider>{content}</EmbeddedProvider> : content);
  return { ...props, onToggleLeftSidebar, onToggleRightSidebar };
}

beforeEach(() => {
  navigate.mockClear();
  useConversations.mockReset();
  setSessions([]);
  localStorage.clear();
  resetKeybindingStoreForTesting();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
  resetKeybindingStoreForTesting();
});

describe("CommandPalette — sessions", () => {
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

  it("debounces the typed query into a server search (archived excluded)", () => {
    vi.useFakeTimers();
    try {
      setSessions([conv("c1", "Fix the parser")]);
      renderPalette();

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
      // archived rows included (filtered client-side) — proving the palette
      // searches the server, not a page.
      expect(useConversations).toHaveBeenCalledWith("deploy", true);
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
    setSessions([conv("c1", "Hello", "cursor", "…can you fix the what if I switch…")]);
    renderPalette();
    search("what");

    // The title stays as the primary line; the snippet shows where it matched.
    expect(screen.getByText("Hello")).toBeTruthy();
    expect(screen.getByText(/can you fix the/)).toBeTruthy();
  });

  it("highlights the query term in both the title and the snippet", () => {
    setSessions([conv("c1", "what model", "cursor", "Hello what model are you using?")]);
    renderPalette();
    search("what");

    // Every occurrence of the query renders inside a <mark> (title + snippet).
    const marks = document.querySelectorAll("mark");
    expect(marks.length).toBeGreaterThanOrEqual(2);
    for (const m of marks) expect(m.textContent?.toLowerCase()).toBe("what");
  });

  it("omits the snippet line for a title-only match (no search_snippet)", () => {
    setSessions([conv("c1", "deploy runbook", "agent", null)]);
    renderPalette();
    search("deploy");

    expect(screen.getByText(/deploy/)).toBeTruthy();
    // No second line: only the single title row is present.
    expect(screen.queryByText(/runbook.*\n/)).toBeNull();
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
      expect(useConversations).toHaveBeenCalledWith("deploy", true);
    } finally {
      vi.useRealTimers();
    }
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
    expect(screen.queryByText("Open command palette")).toBeNull();
    expect(screen.queryByText("Open keyboard shortcuts")).toBeNull();
  });

  it("preserves the historical action order with New chat first", () => {
    renderPalette();
    const group = screen.getByText("Actions").closest("[cmdk-group]");
    const labels = within(group as HTMLElement)
      .getAllByRole("option")
      .map((item) => item.querySelector("[data-action-title]")?.textContent);
    expect(labels).toEqual([
      "New chat",
      "Go to Inbox",
      "Go to Automations",
      "Go to Settings",
      "Toggle conversations sidebar",
      "Toggle workspace sidebar",
    ]);
  });

  it.each([
    ["new session", "New chat"],
    ["account", "Go to Settings"],
    ["sessions list", "Toggle conversations sidebar"],
  ])("retains the legacy %s keyword", (query, label) => {
    renderPalette();
    fireEvent.change(screen.getByTestId("command-palette-input"), { target: { value: query } });
    expect(screen.getByText(label)).toBeTruthy();
  });

  it("shows effective shortcuts and updates them live", () => {
    renderPalette();
    const newChat = screen.getByText("New chat").closest("[data-slot=command-item]") as HTMLElement;
    expect(within(newChat).getByText("Ctrl+N")).toBeInTheDocument();
    act(() => {
      expect(
        setUserKeybindingRule({
          id: "session.new",
          action: "session.action.new",
          sequence: "ctrl+shift+n",
          mode: "global",
        }),
      ).toEqual({ ok: true, changed: true });
    });
    expect(within(newChat).getByText("Ctrl+Shift+N")).toBeInTheDocument();
  });

  it("hides standalone-only hints in embedded mode", () => {
    renderPalette({ embedded: true });
    const newChat = screen.getByText("New chat").closest("[data-slot=command-item]") as HTMLElement;
    expect(within(newChat).queryByText("Ctrl+N")).toBeNull();
  });

  it("runs a navigation action with palette source and closes", () => {
    const onOpenChange = vi.fn();
    const onInvocation = vi.fn();
    renderPalette({ onOpenChange, onInvocation });

    fireEvent.click(screen.getByText("Go to Settings"));

    expect(navigate).toHaveBeenCalledWith("/settings");
    expect(onInvocation).toHaveBeenCalledWith("palette");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("renders disabled actions without executing them", () => {
    renderPalette({ settingsEnabled: false });
    const row = screen.getByText("Go to Settings").closest("[data-slot=command-item]");
    expect(row).toHaveAttribute("data-disabled", "true");
    fireEvent.click(row!);
    expect(navigate).not.toHaveBeenCalledWith("/settings");
  });

  it("omits invisible actions", () => {
    renderPalette({ inboxVisible: false });
    expect(screen.queryByText("Go to Inbox")).toBeNull();
  });

  it("removes rows when their handler unregisters", () => {
    function ToggleHarness() {
      const [registered, setRegistered] = useState(true);
      return (
        <ActionsProvider>
          {registered ? (
            <PaletteActions onToggleLeftSidebar={() => {}} onToggleRightSidebar={() => {}} />
          ) : null}
          <button type="button" onClick={() => setRegistered(false)}>
            Unregister
          </button>
          <CommandPalette open onOpenChange={() => {}} />
        </ActionsProvider>
      );
    }
    render(<ToggleHarness />);
    expect(screen.getByText("New chat")).toBeTruthy();
    fireEvent.click(screen.getByText("Unregister"));
    expect(screen.queryByText("New chat")).toBeNull();
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
