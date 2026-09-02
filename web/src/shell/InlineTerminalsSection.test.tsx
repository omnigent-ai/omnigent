import type * as UseTerminalsModule from "@/hooks/useTerminals";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type TerminalInfo, useTerminals } from "@/hooks/useTerminals";
import type { TerminalFirstContextValue } from "./TerminalFirstContext";
import { TerminalFirstContextProvider } from "./TerminalFirstContext";
import { InlineTerminalsSection } from "./InlineTerminalsSection";

vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  // Keep the real module (inventoryTerminals etc.) — only the
  // network-backed hook is replaced.
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: vi.fn(),
}));

// The "+ New shell" button needs a QueryClient (reads the session agent for
// its access gate); its behavior is covered by NewTerminalButton.test.tsx. A
// marker keeps its presence assertable.
vi.mock("./NewTerminalButton", () => ({
  NewTerminalButton: ({ variant }: { variant?: string }) => (
    <div data-testid="new-shell-button" data-variant={variant} />
  ),
}));

const useTerminalsMock = vi.mocked(useTerminals);

function makeTerminal(id: string, name: string, session: string): TerminalInfo {
  return {
    id,
    name,
    session,
    running: true,
  };
}

/**
 * Minimal TerminalFirst context for a terminal-first SDK session, so
 * the section's inventory filter (REPL excluded) is exercised.
 */
const TERMINAL_FIRST_SDK_CTX = {
  isClaudeNative: false,
  isNativeWrapper: false,
  isTerminalFirst: true,
  isShellView: false,
  view: "chat",
  terminalViewKey: null,
  setView: () => {},
  terminalsAvailable: true,
  terminalStartingUp: false,
} as TerminalFirstContextValue;

function renderInlineSection(
  terminals: TerminalInfo[],
  onExpand: (key: string) => void = vi.fn(),
  { showNewShell = false }: { showNewShell?: boolean } = {},
) {
  useTerminalsMock.mockReturnValue({
    terminals,
    isLoading: false,
    error: null,
  });
  return render(
    <TerminalFirstContextProvider value={TERMINAL_FIRST_SDK_CTX}>
      <InlineTerminalsSection
        conversationId="conv_terminal"
        onExpand={onExpand}
        showNewShell={showNewShell}
      />
    </TerminalFirstContextProvider>,
  );
}

beforeEach(() => {
  useTerminalsMock.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("InlineTerminalsSection rows open shells in the main view", () => {
  it("clicking a shell row hands its key to onExpand instead of mounting an in-rail xterm", () => {
    const onExpand = vi.fn();
    renderInlineSection(
      [
        makeTerminal("terminal_bash_s1", "bash", "s1"),
        makeTerminal("terminal_worker_s2", "worker", "s2"),
      ],
      onExpand,
    );

    fireEvent.click(screen.getByRole("button", { name: /s1/ }));

    // The shell must take over the MAIN view via onExpand — the rail
    // never mounts an xterm of its own. A missing call means rows went
    // back to in-rail selection; a wrong key opens the wrong shell.
    expect(onExpand).toHaveBeenCalledWith("terminal:terminal_bash_s1");
    expect(screen.queryByTestId("terminal-view")).toBeNull();
  });

  it("lists every shell with no selection state", () => {
    renderInlineSection([
      makeTerminal("terminal_bash_s1", "bash", "s1"),
      makeTerminal("terminal_worker_s2", "worker", "s2"),
    ]);

    expect(screen.getByRole("button", { name: /s1/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /s2/ })).toBeInTheDocument();
    expect(screen.queryByTestId("terminal-view")).toBeNull();
  });

  it("excludes the embedded REPL terminal from the shell list", () => {
    renderInlineSection([
      makeTerminal("terminal_tui_main", "tui", "main"),
      makeTerminal("terminal_bash_s1", "bash", "s1"),
    ]);

    // The REPL backs the Chat/Terminal pill — a row for it here is the
    // phantom "main" terminal regression.
    expect(screen.queryByText("main")).toBeNull();
    expect(screen.getByRole("button", { name: /s1/ })).toBeInTheDocument();
  });

  it("renders an empty list (no new-shell row) when the only terminal is the embedded REPL", () => {
    renderInlineSection([makeTerminal("terminal_tui_main", "tui", "main")]);

    // Desktop default: shell creation lives in the tab strip's "+" menu, not
    // here — so no "+ New shell" row, and no centered empty-state copy.
    expect(screen.queryByTestId("new-shell-button")).toBeNull();
    expect(screen.queryByText("No shells running.")).toBeNull();
  });

  it("shows a leading '+ New shell' row when showNewShell is set (mobile drawer)", () => {
    // Mobile has no tab-strip "+" menu, so the drawer opts in to the create row.
    renderInlineSection([makeTerminal("terminal_bash_s1", "bash", "s1")], vi.fn(), {
      showNewShell: true,
    });

    const row = screen.getByTestId("new-shell-button");
    expect(row).toHaveAttribute("data-variant", "row");
    // Leading — it sits before the shell rows so it stays at a fixed spot.
    const shellRow = screen.getByRole("button", { name: /s1/ });
    expect(row.compareDocumentPosition(shellRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
