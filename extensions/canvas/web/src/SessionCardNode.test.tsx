import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { NodeProps, Node } from "@xyflow/react";
import { SessionCardNode, type SessionCardData } from "./SessionCardNode";

function renderCard(overrides: Partial<SessionCardData["session"]> = {}) {
  const onOpen = vi.fn();
  const session = {
    id: "conv_1",
    title: "Fix authentication",
    status: "running" as const,
    workspace: "/workspace/project",
    createdAt: 1,
    updatedAt: 2,
    ...overrides,
  };
  const props = {
    id: session.id,
    data: { session, onOpen },
    selected: false,
  } as unknown as NodeProps<Node<SessionCardData>>;
  render(<SessionCardNode {...props} />);
  return { onOpen };
}

describe("SessionCardNode", () => {
  it("shows title, text status, and working directory", () => {
    renderCard();
    expect(screen.getByText("Fix authentication")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("/workspace/project")).toBeInTheDocument();
    expect(screen.getByRole("button")).toHaveAccessibleName(
      "Fix authentication. Running. /workspace/project",
    );
  });

  it("uses explicit fallbacks for missing card fields", () => {
    renderCard({ title: null, workspace: null, status: "idle" });
    expect(screen.getByText("Untitled session")).toBeInTheDocument();
    expect(screen.getByText("Idle")).toBeInTheDocument();
    expect(screen.getByText("No working directory")).toBeInTheDocument();
  });

  it.each(["Enter", " "])("opens from the %p key", (key) => {
    const { onOpen } = renderCard();
    fireEvent.keyDown(screen.getByRole("button"), { key });
    expect(onOpen).toHaveBeenCalledOnce();
    expect(onOpen).toHaveBeenCalledWith("conv_1");
  });

  it("opens from an assistive-technology click", () => {
    const { onOpen } = renderCard();
    fireEvent.click(screen.getByRole("button"), { detail: 0 });
    expect(onOpen).toHaveBeenCalledWith("conv_1");
  });

  it("does not open from a single pointer click", () => {
    const { onOpen } = renderCard();
    fireEvent.click(screen.getByRole("button"), { detail: 1 });
    expect(onOpen).not.toHaveBeenCalled();
  });
});
