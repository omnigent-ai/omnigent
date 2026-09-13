import type * as UseTerminalsModule from "@/hooks/useTerminals";

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type TerminalInfo, useTerminals } from "@/hooks/useTerminals";
import { TerminalsPanel } from "./TerminalsPanel";

// Monotonic per-mount id. A fresh `data-instance` means React remounted
// the TerminalView (new xterm + WebSocket) rather than reusing it — the
// signal the stale-scrollback regression test needs.
let terminalMountSeq = 0;

vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: ({
    sessionId,
    terminalId,
    readOnly,
  }: {
    sessionId: string;
    terminalId: string;
    readOnly?: boolean;
  }) => {
    // Assign once per mount (useRef(arg) evaluates arg every render but keeps
    // the first value), so the id is stable across re-renders and only changes
    // on a remount.
    const instance = useRef<number | null>(null);
    if (instance.current === null) instance.current = ++terminalMountSeq;
    return (
      <div
        data-testid="terminal-view"
        data-session-id={sessionId}
        data-terminal-id={terminalId}
        data-read-only={String(readOnly ?? false)}
        data-instance={String(instance.current)}
      />
    );
  },
}));

vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  // Keep the real module (inventoryTerminals etc.) — only the
  // network-backed hook is replaced.
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: vi.fn(),
}));

// These tests cover panel navigation, not terminal creation. The
// button needs a QueryClient (it reads the session agent for its
// access gate); its behavior is covered by NewTerminalButton.test.tsx.
vi.mock("./NewTerminalButton", () => ({
  NewTerminalButton: () => null,
}));

const useTerminalsMock = vi.mocked(useTerminals);
const originalMatchMedia = window.matchMedia;

function makeTerminal(id: string, name: string, session: string): TerminalInfo {
  return {
    id,
    name,
    session,
    running: true,
  };
}

function mockTerminalList(terminals: TerminalInfo[]) {
  useTerminalsMock.mockReturnValue({
    terminals,
    isLoading: false,
    error: null,
  });
}

function renderPanel({
  initialTerminalKey = null,
  readOnly = false,
  terminals = [
    makeTerminal("terminal_main", "main", "s1"),
    makeTerminal("terminal_worker", "worker", "s2"),
  ],
}: {
  initialTerminalKey?: string | null;
  readOnly?: boolean;
  terminals?: TerminalInfo[];
} = {}) {
  mockTerminalList(terminals);
  return render(
    <TerminalsPanel
      open
      conversationId="conv_terminal"
      initialTerminalKey={initialTerminalKey}
      readOnly={readOnly}
      onClose={vi.fn()}
    />,
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  useTerminalsMock.mockReset();
  window.matchMedia = vi.fn((query: string) => ({
    matches: query === "(min-width: 768px)",
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
  window.matchMedia = originalMatchMedia;
});

describe("TerminalsPanel resize handle geometry", () => {
  it("renders the handle as the panel's unclipped boundary sibling", () => {
    renderPanel();
    const handle = screen.getByRole("separator", { name: "Resize panel" });
    const panel = screen.getByTestId("terminals-panel");

    expect(handle.nextElementSibling).toBe(panel);
    expect(panel.contains(handle)).toBe(false);
    expect(handle.closest(".overflow-hidden, .overflow-auto, .overflow-y-auto")).toBeNull();
    expect(handle.className).toMatch(/\bz-10\b/);
  });
});

describe("TerminalsPanel navigation", () => {
  it("opens to the list view with all terminals visible and no terminal mounted", () => {
    renderPanel();

    // Both rows are always visible in the left list.
    expect(screen.getByRole("button", { name: /main/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /worker/i })).toBeInTheDocument();
    // No xterm until a terminal is selected.
    expect(screen.queryByTestId("terminal-view")).toBeNull();
  });

  it("shows terminal view after clicking a row, deferred until expanded", async () => {
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /worker/i }));

    // List rows still visible in the left panel (split layout).
    expect(screen.getByRole("button", { name: /main/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /worker/i })).toBeInTheDocument();
    // TerminalView deferred until 180 ms settle.
    expect(screen.queryByTestId("terminal-view")).toBeNull();

    // async act flushes both the fake-timer tick and any Suspense microtasks
    // from the lazy TerminalView chunk resolving through its boundary.
    await act(async () => {
      vi.advanceTimersByTime(180);
    });

    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_worker",
    );
    expect(screen.getByTestId("terminal-view")).toHaveAttribute("data-session-id", "conv_terminal");
  });

  it("deselects terminal and hides TerminalView when active row is clicked again", () => {
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /main/i }));
    act(() => {
      vi.advanceTimersByTime(180);
    });
    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_main",
    );

    // Click the active row again to toggle back to list-only.
    fireEvent.click(screen.getByRole("button", { name: /main/i }));

    expect(screen.queryByTestId("terminal-view")).toBeNull();
  });

  it("falls back to the list view for a stale initial terminal key", () => {
    renderPanel({ initialTerminalKey: "terminal:terminal_removed" });

    expect(screen.getByRole("button", { name: /main/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /worker/i })).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(180);
    });

    expect(screen.queryByTestId("terminal-view")).toBeNull();
  });

  it("forwards readOnly so non-owners attach shells view-only", () => {
    renderPanel({ initialTerminalKey: "terminal:terminal_main", readOnly: true });

    act(() => {
      vi.advanceTimersByTime(180);
    });

    // A non-owner sees the shell but cannot type — the shared PTY runs
    // as the owner, so keystrokes can't be attributed per-user.
    expect(screen.getByTestId("terminal-view")).toHaveAttribute("data-read-only", "true");
  });

  it("remounts the terminal when switching sessions with the same terminal id", () => {
    // Two sessions of the same shape share a terminal id (e.g. every
    // native session's agent pane). The panel stays mounted across a
    // session switch, so keying the xterm wrapper on the id alone would
    // reuse the mount and keep the previous session's scrollback until
    // the new WS repaints. The wrapper key is scoped to the session id.
    const terminals = [makeTerminal("terminal_claude_main", "claude", "main")];
    const { rerender } = renderPanel({
      initialTerminalKey: "terminal:terminal_claude_main",
      terminals,
    });
    act(() => {
      vi.advanceTimersByTime(180);
    });
    const first = screen.getByTestId("terminal-view").getAttribute("data-instance");

    rerender(
      <TerminalsPanel
        open
        conversationId="conv_other"
        initialTerminalKey="terminal:terminal_claude_main"
        readOnly={false}
        onClose={vi.fn()}
      />,
    );
    act(() => {
      vi.advanceTimersByTime(180);
    });

    const view = screen.getByTestId("terminal-view");
    expect(view).toHaveAttribute("data-session-id", "conv_other");
    // A new instance id proves a clean remount, not a reused mount with
    // stale scrollback.
    expect(view.getAttribute("data-instance")).not.toBe(first);
  });

  it("defers mounting TerminalView until the panel is expanded", () => {
    renderPanel({ initialTerminalKey: "terminal:terminal_main" });

    // TerminalView is deferred until the 180 ms layout-settle timeout fires.
    expect(screen.queryByTestId("terminal-view")).toBeNull();

    act(() => {
      vi.advanceTimersByTime(179); // one ms before the threshold — still deferred
    });
    expect(screen.queryByTestId("terminal-view")).toBeNull();

    act(() => {
      vi.advanceTimersByTime(1); // threshold reached — TerminalView mounts
    });

    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_main",
    );
  });
});

describe("TerminalsPanel column resize handle", () => {
  // jsdom has no layout engine, so these tests assert the structural
  // invariants that produce the desired geometry in a real browser: the
  // handle must live OUTSIDE the vertically scrolling list panel (a pad
  // inside it gets clipped and makes the list pannable horizontally) and
  // sit on the column boundary via `left: <list width>`.
  function getHandle() {
    return screen.getByRole("separator", { name: /resize terminal list/i });
  }

  function getListPanel() {
    // The list panel is the scrollable element containing the row buttons.
    const row = screen.getByRole("button", { name: /main/i });
    const panel = row.parentElement as HTMLElement;
    expect(panel.className).toContain("overflow-y-auto");
    return panel;
  }

  it("renders the handle at the boundary, outside the scrollable list panel", () => {
    renderPanel({ initialTerminalKey: "terminal:terminal_main" });

    const handle = getHandle();
    const listPanel = getListPanel();

    // Sibling of the list panel inside the overflow-hidden split row — never
    // a descendant of the scroll container that would clip its hit pad.
    expect(listPanel.contains(handle)).toBe(false);
    expect(handle.parentElement).toBe(listPanel.parentElement);

    // Anchored on the column boundary with the drag/touch affordances live.
    expect(handle.style.left).toBe("176px");
    expect(handle.style.touchAction).toBe("none");
    expect(handle.style.paddingLeft).not.toBe("");
  });

  it("keeps terminal rows selectable outside the handle pad", () => {
    renderPanel({ initialTerminalKey: "terminal:terminal_main" });

    // Tapping the body of another row (well clear of the boundary pad) must
    // still switch the active terminal.
    fireEvent.click(screen.getByRole("button", { name: /worker/i }));
    act(() => {
      vi.advanceTimersByTime(200); // past the layout-settle mount deferral
    });
    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_worker",
    );
  });

  it("removes the handle when the panel closes (no focusable separator in aria-hidden)", () => {
    const { rerender } = renderPanel({ initialTerminalKey: "terminal:terminal_main" });
    expect(getHandle()).toBeInTheDocument();

    rerender(
      <TerminalsPanel
        open={false}
        conversationId="conv_terminal"
        initialTerminalKey={null}
        readOnly={false}
        onClose={vi.fn()}
      />,
    );

    // The closed aside is aria-hidden; a tabbable separator inside it would
    // be an ARIA violation and would let keyboard users resize the
    // off-screen column.
    expect(
      screen.queryByRole("separator", { name: /resize terminal list/i, hidden: true }),
    ).toBeNull();
    const panel = screen.getByTestId("terminals-panel");
    expect(panel).toHaveAttribute("aria-hidden", "true");
    expect(panel.querySelector('[tabindex="0"][role="separator"]')).toBeNull();
  });

  it("adds no horizontally overflowing content to the list panel", () => {
    renderPanel({ initialTerminalKey: "terminal:terminal_main" });

    // The only element wider than the column (the invisible hit pad) must not
    // be inside the list panel; everything the panel contains is w-full rows.
    const listPanel = getListPanel();
    for (const child of Array.from(listPanel.children)) {
      expect((child as HTMLElement).style.paddingLeft).toBe("");
      expect(child.getAttribute("role")).not.toBe("separator");
    }
  });
});
