import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SessionCloseShellDialog } from "./SessionWorkspacePanel";
import type { SessionWorkspace } from "./useSessionWorkspace";

function workspaceClosing(keys: string[]) {
  const terminals = keys.map((key) => ({ id: `id-${key}`, name: `shell ${key}`, key }));
  const sessionWorkspace = {
    pendingClose: { keys, callbacks: { onConfirmed: vi.fn(), onFailed: vi.fn() } },
    setPendingClose: vi.fn(),
    terminalsToClose: terminals,
    setKillingTerminalIds: vi.fn(),
    deleteTerminal: { mutateAsync: vi.fn(() => Promise.resolve()) },
    workspace: { selectedTerminalKey: null },
    updateWorkspace: vi.fn(),
  };
  return sessionWorkspace;
}

vi.mock("@/hooks/useTerminals", () => ({
  terminalTabKey: (terminal: { key: string }) => terminal.key,
}));

describe("SessionCloseShellDialog", () => {
  it("kills every confirmed shell and closes", async () => {
    const sessionWorkspace = workspaceClosing(["a", "b"]);
    render(
      <SessionCloseShellDialog
        sessionWorkspace={sessionWorkspace as unknown as SessionWorkspace}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Close shells" }));

    expect(sessionWorkspace.deleteTerminal.mutateAsync).toHaveBeenCalledWith("id-a");
    expect(sessionWorkspace.deleteTerminal.mutateAsync).toHaveBeenCalledWith("id-b");
    expect(sessionWorkspace.pendingClose.callbacks.onConfirmed).toHaveBeenCalled();
    expect(sessionWorkspace.setPendingClose).toHaveBeenCalledWith(null);
    await waitFor(() => expect(sessionWorkspace.setKillingTerminalIds).toHaveBeenCalledTimes(4));
  });

  it("keeps the shells when cancelled", () => {
    const sessionWorkspace = workspaceClosing(["a"]);
    render(
      <SessionCloseShellDialog
        sessionWorkspace={sessionWorkspace as unknown as SessionWorkspace}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(sessionWorkspace.deleteTerminal.mutateAsync).not.toHaveBeenCalled();
    expect(sessionWorkspace.setPendingClose).toHaveBeenCalledWith(null);
  });
});
