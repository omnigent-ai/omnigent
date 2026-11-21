import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as UseTerminalsModule from "@/hooks/useTerminals";
import type { TerminalInfo } from "@/lib/terminals";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import { WorkspaceTabsHost, type WorkspaceTabsHostProps } from "./WorkspaceTabsHost";
import type { WorkspaceTabsPanel } from "./WorkspaceTabsPanel";
import { resetWorkspaceTabsSessionsForTests, workspaceTabsSession } from "./workspaceTabsSessions";

const captured = vi.hoisted(() => ({
  panel: null as ComponentProps<typeof WorkspaceTabsPanel> | null,
  deleteTerminal: vi.fn(async (_id: string) => undefined),
}));
vi.mock("./WorkspaceTabsPanel", () => ({
  WorkspaceTabsPanel: (props: ComponentProps<typeof WorkspaceTabsPanel>) => {
    captured.panel = props;
    return <div data-testid="tabs-panel" />;
  },
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useDeleteTerminal: () => ({ mutateAsync: captured.deleteTerminal }),
}));

const ZSH: TerminalInfo = { id: "t1", name: "zsh", session: "s1", running: true };

function host(overrides: Partial<WorkspaceTabsHostProps> = {}) {
  const onReveal = vi.fn();
  const props = {
    conversationId: "conv_host",
    width: 360,
    handleProps: { tabIndex: 0 },
    showFilesPanel: true,
    showGithubTab: true,
    showBrowserTab: false,
    changedCount: 0,
    changedFiles: [],
    subagentsWorking: 0,
    agentCount: 1,
    rootSessionId: null,
    terminalKeys: ["terminal:t1"],
    openTerminalTab: vi.fn(),
    maximized: false,
    onToggleMaximized: vi.fn(),
    permissionLevel: null,
    filesPanelSort: "alpha",
    onSortChange: vi.fn(),
    filesPanelShowHidden: false,
    onShowHiddenChange: vi.fn(),
    terminals: [ZSH],
    onReveal,
    ...overrides,
  } as WorkspaceTabsHostProps;
  render(<WorkspaceTabsHost {...props} />);
  return { onReveal };
}

beforeEach(() => {
  captured.panel = null;
  captured.deleteTerminal.mockClear();
});
afterEach(() => {
  cleanup();
  resetWorkspaceTabsSessionsForTests();
  window.localStorage.clear();
});

describe("WorkspaceTabsHost", () => {
  it("asks in the Close shell dialog before a shell's tab closes, then ends it", async () => {
    const user = userEvent.setup();
    host();
    let closing!: Promise<boolean>;
    act(() => {
      closing = captured.panel!.confirmCloseShell("terminal:t1");
    });
    expect(await screen.findByText("zsh · s1")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close shell" }));
    await expect(closing).resolves.toBe(true);

    await expect(captured.panel!.endShell("terminal:t1")).resolves.toBe(true);
    expect(captured.deleteTerminal).toHaveBeenCalledWith("t1");
  });

  it("binds Back and Forward, opening the sidebar before a step", async () => {
    // Files, then Changes in a new tab, so Back lands on Files.
    const { store } = workspaceTabsSession("conv_host");
    await act(() => store.open({ place: { kind: "files" } }, { opener: "user" }));
    await act(() =>
      store.open({ place: { kind: "changes" } }, { opener: "user", disposition: "new" }),
    );
    const placeShown = () => store.selectedTab()?.place.kind;
    const steps: string[] = [];
    const onReveal = vi.fn(() => steps.push(`reveal on ${placeShown()}`));
    host({ onReveal });
    expect(captured.panel?.historyCommands?.map((command) => command.id)).toEqual([
      "history:back",
      "history:forward",
    ]);

    await act(async () => captured.panel!.onHistoryGo!("back"));
    await vi.waitFor(() => expect(placeShown()).toBe("files"));
    expect(steps).toEqual(["reveal on changes"]);
    expect(
      registeredPaletteCommands().some((row) => row.id === "workspace:conv_host:history:back"),
    ).toBe(true);
  });

  it("leaves Back and Forward to the cell that binds them", () => {
    host({ bindsHistory: false });
    expect(captured.panel?.onHistoryGo).toBeUndefined();
    expect(registeredPaletteCommands().some((row) => row.id.includes("history"))).toBe(false);
  });
});
