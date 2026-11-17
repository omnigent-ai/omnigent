import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ActiveTileWorkspace } from "./ActiveTileWorkspace";

const sessionState = vi.hoisted(() => ({ runnerId: null as string | null }));

vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: { id: "conv_a", runnerId: sessionState.runnerId } }),
  useRootSessionId: () => "conv_a",
}));
vi.mock("@/shell/useSessionWorkspace", () => ({
  useSessionWorkspace: () => {
    throw new Error("workspace requested for a session without a runner");
  },
}));

const panel = { open: true, width: 320, maximized: false, onToggleMaximized: () => {} };

describe("ActiveTileWorkspace", () => {
  it("doesn't request workspace data until the session has a runner", () => {
    render(
      <ActiveTileWorkspace
        sessionId="conv_a"
        workspacePanel={panel as never}
        onOpenWorkspace={() => {}}
        onFileContext={() => {}}
        onSwitchGuard={() => {}}
      />,
    );
    expect(screen.getByTestId("tiles-workspace-not-running")).toHaveTextContent(
      "Agent isn't running",
    );
  });

  it("renders nothing while the drawer is closed", () => {
    const { container } = render(
      <ActiveTileWorkspace
        sessionId="conv_a"
        workspacePanel={{ ...panel, open: false } as never}
        onOpenWorkspace={() => {}}
        onFileContext={() => {}}
        onSwitchGuard={() => {}}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
