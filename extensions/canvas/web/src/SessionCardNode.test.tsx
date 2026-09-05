import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { NodeProps, Node } from "@xyflow/react";
import { SessionCardNode, type SessionCardData } from "./SessionCardNode";

function renderCard(
  overrides: Partial<SessionCardData["session"]> = {},
  extra: Partial<Pick<SessionCardData, "pullRequest" | "onOpenExternal">> = {},
) {
  const onOpen = vi.fn();
  const session = {
    id: "conv_1",
    title: "Fix authentication",
    status: "running" as const,
    unread: false,
    titleProvisional: false,
    gitBranch: null,
    workspace: "/workspace/project",
    projectId: null,
    createdAt: 1,
    updatedAt: 2,
    ...overrides,
  };
  const props = {
    id: session.id,
    data: { session, onOpen, ...extra },
    selected: false,
  } as unknown as NodeProps<Node<SessionCardData>>;
  render(<SessionCardNode {...props} />);
  return { onOpen, card: screen.getAllByRole("button")[0] };
}

describe("SessionCardNode", () => {
  it("shows title, text status, and working directory", () => {
    const { card } = renderCard();
    expect(screen.getByText("Fix authentication")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("/workspace/project")).toBeInTheDocument();
    expect(card).toHaveAccessibleName(
      "Fix authentication. Running. /workspace/project",
    );
  });

  it.each(["running", "waiting"] as const)(
    "spins like the sidebar while the session is %s",
    (status) => {
      const { card } = renderCard({ status });
      expect(card).toHaveAttribute("data-state", status);
      expect(card.querySelector(".session-spinner")).not.toBeNull();
      expect(card.querySelector(".session-status-dot")).toBeNull();
    },
  );

  it("shows the sidebar's unread dot for finished output the user has not seen", () => {
    const { card } = renderCard({ status: "idle", unread: true });
    expect(card).toHaveAttribute("data-state", "unread");
    expect(card.querySelector(".session-status-dot")).not.toBeNull();
    expect(card.querySelector(".session-spinner")).toBeNull();
    expect(screen.getByText("New messages")).toBeInTheDocument();
    expect(card).toHaveAccessibleName(
      "Fix authentication. New messages. /workspace/project",
    );
  });

  it.each(["idle", "failed"] as const)(
    "shows a plain gray dot for a %s session that has been seen",
    (status) => {
      const { card } = renderCard({ status });
      expect(card).toHaveAttribute("data-state", "idle");
      expect(card.querySelector(".session-status-dot")).not.toBeNull();
      expect(screen.getByText("Idle")).toBeInTheDocument();
    },
  );

  it("renders a provisional first-message title like the sidebar row", () => {
    const { card } = renderCard({ title: "Bob", titleProvisional: true });
    expect(card.querySelector("strong")).toHaveClass(
      "session-card-title-provisional",
    );
    expect(screen.getByText("Bob")).toBeInTheDocument();
  });

  it("shows the worktree branch under the workspace when the session has one", () => {
    const { card } = renderCard({ gitBranch: "feat/canvas" });
    expect(screen.getByText("feat/canvas")).toBeInTheDocument();
    expect(card.querySelector(".session-branch svg")).not.toBeNull();
    expect(card.querySelector(".session-workspace svg")).not.toBeNull();
  });

  it("links the filed pull request and opens it through the host", () => {
    const onOpenExternal = vi.fn();
    renderCard(
      {},
      {
        pullRequest: {
          number: 7,
          title: "Ship it",
          state: "OPEN",
          url: "https://github.com/a/b/pull/7",
        },
        onOpenExternal,
      },
    );
    const link = screen.getByRole("button", { name: "Open pull request #7" });
    expect(link).toHaveTextContent("#7 Ship it");
    fireEvent.click(link);
    expect(onOpenExternal).toHaveBeenCalledWith(
      "https://github.com/a/b/pull/7",
    );
  });

  it("uses explicit fallbacks for missing card fields", () => {
    renderCard({ title: null, workspace: null, status: "idle" });
    expect(screen.getByText("Untitled session")).toBeInTheDocument();
    expect(screen.getByText("Idle")).toBeInTheDocument();
    expect(screen.getByText("No working directory")).toBeInTheDocument();
  });

  it.each(["Enter", " "])("opens from the %p key", (key) => {
    const { onOpen, card } = renderCard();
    fireEvent.keyDown(card, { key });
    expect(onOpen).toHaveBeenCalledOnce();
    expect(onOpen).toHaveBeenCalledWith("conv_1");
  });

  it("opens from an assistive-technology click", () => {
    const { onOpen, card } = renderCard();
    fireEvent.click(card, { detail: 0 });
    expect(onOpen).toHaveBeenCalledWith("conv_1");
  });

  it("does not open from a single pointer click", () => {
    const { onOpen, card } = renderCard();
    fireEvent.click(card, { detail: 1 });
    expect(onOpen).not.toHaveBeenCalled();
  });
});
