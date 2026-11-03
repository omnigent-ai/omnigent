import type * as UseTerminalsModule from "@/hooks/useTerminals";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { writePreferredShell } from "./preferredShell";
import { TerminalSplitControls } from "./TerminalSplitControls";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "./TerminalFirstContext";

const AGENT = { id: "terminal_claude_main", name: "claude", session: "main", running: true };
const BASH = { id: "terminal_bash_u-1", name: "bash", session: "u-1", running: true };
const AGENT_KEY = "terminal:terminal_claude_main";
const BASH_KEY = "terminal:terminal_bash_u-1";

const fixture = vi.hoisted(() => ({
  createTerminal: vi.fn(),
  createPending: false,
  deleteTerminal: vi.fn(),
  declaredShells: ["zsh", "bash"],
  mobile: false,
  terminals: [] as UseTerminalsModule.TerminalInfo[],
}));

vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: () => ({ data: { terminals: fixture.declaredShells } }),
}));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => fixture.mobile }));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: () => ({ terminals: fixture.terminals }),
  useCreateTerminal: () => ({ mutate: fixture.createTerminal, isPending: fixture.createPending }),
  useDeleteTerminal: () => ({ mutate: fixture.deleteTerminal, isPending: false }),
}));

beforeEach(() => {
  fixture.createTerminal.mockReset();
  fixture.deleteTerminal.mockReset();
  fixture.createPending = false;
  fixture.declaredShells = ["zsh", "bash"];
  fixture.mobile = false;
  fixture.terminals = [AGENT];
});
afterEach(cleanup);

function renderControls(
  conversationId: string,
  {
    enabled = true,
    permissionLevel = null as number | null,
    context = {} as Partial<TerminalFirstContextValue>,
  } = {},
) {
  const terminalFirst = {
    isTerminalFirst: true,
    isShellView: false,
    view: "terminal",
    ...context,
  } as TerminalFirstContextValue;
  return render(
    <CapabilitiesProvider
      info={{ ...FALLBACK_SERVER_INFO, features: { splittable_terminal_view_ui: enabled } }}
    >
      <TerminalFirstContextProvider value={terminalFirst}>
        <TooltipProvider>
          <TerminalSplitControls
            conversationId={conversationId}
            permissionLevel={permissionLevel}
          />
        </TooltipProvider>
      </TerminalFirstContextProvider>
    </CapabilitiesProvider>,
  );
}

it.each([
  ["the feature is off", () => renderControls("conv_gate", { enabled: false })],
  [
    "the viewport is mobile",
    () => {
      fixture.mobile = true;
      return renderControls("conv_gate");
    },
  ],
  ["the viewer is not an owner", () => renderControls("conv_gate", { permissionLevel: 1 })],
  [
    "a rail shell owns the view",
    () => renderControls("conv_gate", { context: { isShellView: true } }),
  ],
  ["chat is shown", () => renderControls("conv_gate", { context: { view: "chat" } })],
  [
    "the agent declares no shells",
    () => {
      fixture.declaredShells = [];
      return renderControls("conv_gate");
    },
  ],
])("renders nothing and leaves the split chords unbound when %s", (_case, renderCase) => {
  renderCase();
  expect(screen.queryByRole("button", { name: "Split right" })).toBeNull();
  fireEvent.keyDown(window, { code: "Backslash", ctrlKey: true, altKey: true, shiftKey: true });
  fireEvent.keyDown(window, { code: "Minus", ctrlKey: true, altKey: true });
  expect(fixture.createTerminal).not.toHaveBeenCalled();
});

it("splits the agent pane with the picked shell and focuses the new pane", async () => {
  fixture.createTerminal.mockImplementation((_shell: string, { onSuccess }) => onSuccess(BASH));
  renderControls("conv_controls_split");

  // Radix menus open on pointerdown, not click.
  const trigger = screen.getByRole("button", { name: "Split down" });
  fireEvent.pointerDown(trigger, { button: 0 });
  expect(await screen.findByRole("menuitem", { name: "zsh (default)" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("menuitem", { name: "bash" }));

  expect(fixture.createTerminal).toHaveBeenCalledWith("bash", expect.anything());
  // Focus must not return to the trigger, where typing would reopen the menu.
  await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  expect(trigger).not.toHaveFocus();
  await waitFor(() =>
    expect(readSessionWorkspaceState("conv_controls_split").terminalSplit).toEqual({
      layout: { axis: "rows", ratio: 0.5, children: [AGENT_KEY, BASH_KEY] },
      focusedKey: BASH_KEY,
    }),
  );
});

it("splits right with the preferred shell from the keyboard, skipping the picker", async () => {
  fixture.createTerminal.mockImplementation((_shell: string, { onSuccess }) => onSuccess(BASH));
  writePreferredShell("bash");
  renderControls("conv_controls_hotkey");

  fireEvent.keyDown(window, { code: "Backslash", ctrlKey: true, altKey: true, shiftKey: true });

  expect(fixture.createTerminal).toHaveBeenCalledExactlyOnceWith("bash", expect.anything());
  expect(screen.queryByRole("menu")).toBeNull();
  await waitFor(() =>
    expect(readSessionWorkspaceState("conv_controls_hotkey").terminalSplit).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT_KEY, BASH_KEY] },
      focusedKey: BASH_KEY,
    }),
  );
});

it("claims the split chord without a second shell while one is being created", () => {
  fixture.createPending = true;
  renderControls("conv_controls_pending");

  // false: the chord was claimed, so a focused terminal never receives it.
  expect(fireEvent.keyDown(window, { code: "Minus", ctrlKey: true, altKey: true })).toBe(false);
  expect(fixture.createTerminal).not.toHaveBeenCalled();
});

function saveSplit(conversationId: string, focusedKey: string) {
  writeSessionWorkspaceState(conversationId, {
    terminalSplit: {
      layout: { axis: "columns", ratio: 0.5, children: [AGENT_KEY, BASH_KEY] },
      focusedKey,
    },
  });
}

it("closes the focused shell only after confirmation", async () => {
  fixture.terminals = [AGENT, BASH];
  saveSplit("conv_controls_close", BASH_KEY);
  renderControls("conv_controls_close");

  fireEvent.click(screen.getByRole("button", { name: "Close pane" }));
  fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
  expect(fixture.deleteTerminal).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Close pane" }));
  fireEvent.click(await screen.findByRole("button", { name: "Close shell" }));
  expect(fixture.deleteTerminal).toHaveBeenCalledWith(BASH.id);
});

it("offers no close for the agent pane", () => {
  fixture.terminals = [AGENT, BASH];
  saveSplit("conv_controls_agent_focus", AGENT_KEY);
  renderControls("conv_controls_agent_focus");

  expect(screen.getByRole("button", { name: "Split right" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Close pane" })).toBeNull();
});

it("drops an unlisted shell pane without killing anything", () => {
  saveSplit("conv_controls_missing", BASH_KEY);
  renderControls("conv_controls_missing");

  fireEvent.click(screen.getByRole("button", { name: "Close pane" }));

  expect(fixture.deleteTerminal).not.toHaveBeenCalled();
  expect(readSessionWorkspaceState("conv_controls_missing").terminalSplit).toBeUndefined();
});
