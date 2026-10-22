import type * as UseTerminalsModule from "@/hooks/useTerminals";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { readSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { TerminalSplitControls } from "./TerminalSplitControls";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "./TerminalFirstContext";

const createTerminal = vi.hoisted(() => vi.fn());

vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: () => ({ data: { terminals: ["zsh", "bash"] } }),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: () => ({
    terminals: [{ id: "terminal_claude_main", name: "claude", session: "main", running: true }],
  }),
  useCreateTerminal: () => ({ mutate: createTerminal, isPending: false }),
  useDeleteTerminal: () => ({ mutate: vi.fn(), isPending: false }),
}));

afterEach(cleanup);

function renderControls(conversationId: string, enabled: boolean) {
  const terminalFirst = {
    isTerminalFirst: true,
    isShellView: false,
    view: "terminal",
  } as TerminalFirstContextValue;
  return render(
    <CapabilitiesProvider
      info={{ ...FALLBACK_SERVER_INFO, features: { splittable_terminal_view_ui: enabled } }}
    >
      <TerminalFirstContextProvider value={terminalFirst}>
        <TooltipProvider>
          <TerminalSplitControls conversationId={conversationId} />
        </TooltipProvider>
      </TerminalFirstContextProvider>
    </CapabilitiesProvider>,
  );
}

it("renders nothing while the feature is off", () => {
  renderControls("conv_controls_off", false);
  expect(screen.queryByRole("button", { name: "Split right" })).toBeNull();
});

it("splits the agent pane with the picked shell and focuses the new pane", async () => {
  createTerminal.mockImplementation((_shell: string, { onSuccess }) =>
    onSuccess({ id: "terminal_bash_u-1", name: "bash", session: "u-1", running: true }),
  );
  renderControls("conv_controls_split", true);

  // Radix menus open on pointerdown, not click.
  const trigger = screen.getByRole("button", { name: "Split down" });
  fireEvent.pointerDown(trigger, { button: 0 });
  expect(await screen.findByRole("menuitem", { name: "zsh (default)" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("menuitem", { name: "bash" }));

  expect(createTerminal).toHaveBeenCalledWith("bash", expect.anything());
  // Focus must not return to the trigger, where typing would reopen the menu.
  await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  expect(trigger).not.toHaveFocus();
  await waitFor(() =>
    expect(readSessionWorkspaceState("conv_controls_split").terminalSplit).toEqual({
      layout: {
        axis: "rows",
        ratio: 0.5,
        children: ["terminal:terminal_claude_main", "terminal:terminal_bash_u-1"],
      },
      focusedKey: "terminal:terminal_bash_u-1",
    }),
  );
});
