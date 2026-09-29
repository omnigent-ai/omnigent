import { fireEvent, render, screen } from "@testing-library/react";
import type { NodeProps } from "@xyflow/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import { resetReadStateForTests, seedReadState } from "@/hooks/useUnseenConversations";
import { clearOptimisticTitles, recordOptimisticTitle } from "@/lib/optimisticTitles";
import { SessionCard, type SessionCardData, type SessionCardNode } from "./SessionCard";

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "conv_1",
    object: "conversation",
    title: "Fix authentication",
    created_at: 1,
    updated_at: 100,
    labels: {},
    permission_level: null,
    status: "running",
    workspace: "/workspace/project",
    ...overrides,
  };
}

function renderCard(
  data: Partial<SessionCardData> & { conversation: Conversation },
  selected = false,
) {
  const props = {
    id: data.conversation.id,
    data: { pullRequest: null, onOpen: vi.fn(), ...data },
    selected,
  } as unknown as NodeProps<SessionCardNode>;
  render(
    <TooltipProvider>
      <SessionCard {...props} />
    </TooltipProvider>,
  );
  return { card: screen.getByTestId("session-card"), onOpen: props.data.onOpen };
}

afterEach(() => {
  resetReadStateForTests();
  clearOptimisticTitles();
});

describe("SessionCard", () => {
  it("shows the title, state text, working directory, and the sidebar's running spinner", () => {
    const { card } = renderCard({ conversation: conversation() });
    expect(screen.getByText("Fix authentication")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("/workspace/project")).toBeInTheDocument();
    expect(card).toHaveAttribute("data-state", "running");
    expect(screen.getByTestId("running-dot")).toBeInTheDocument();
    expect(card).toHaveAccessibleName("Fix authentication. Running. /workspace/project");
  });

  it("outranks everything with a pending approval", () => {
    const { card } = renderCard({
      conversation: conversation({ status: "idle", pending_elicitations_count: 2 }),
    });
    expect(card).toHaveAttribute("data-state", "awaiting");
    expect(screen.getAllByText("Needs response").length).toBeGreaterThan(0);
  });

  it("shows the unread dot for finished output the user has not seen", () => {
    // The list seeds the viewer's last-seen baseline; a later updated_at reads as unseen.
    seedReadState([{ id: "conv_1", viewer_last_seen: 50, updated_at: 100 }]);
    const { card } = renderCard({
      conversation: conversation({ status: "idle", updated_at: 100 }),
    });
    expect(card).toHaveAttribute("data-state", "unseen");
    expect(screen.getByText("New messages")).toBeInTheDocument();
  });

  it("falls back to idle and failed labels", () => {
    renderCard({ conversation: conversation({ status: "idle" }) });
    expect(screen.getByTestId("session-card")).toHaveAttribute("data-state", "idle");
    expect(screen.getByText("Idle")).toBeInTheDocument();
  });

  it("renders a provisional first-message title like the sidebar row", () => {
    recordOptimisticTitle("conv_1", "Bob");
    const { card } = renderCard({ conversation: conversation({ title: null }) });
    expect(screen.getByText("Bob")).toHaveClass("italic");
    expect(card).toHaveAccessibleName(/^Bob\./);
  });

  it("shows the worktree branch and links the open pull request", () => {
    renderCard({
      conversation: conversation({ git_branch: "feat/canvas" }),
      pullRequest: {
        number: 7,
        title: "Ship it",
        state: "OPEN",
        url: "https://github.com/acme/repo/pull/7",
      },
    });
    expect(screen.getByText("feat/canvas")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Open pull request #7" });
    expect(link).toHaveAttribute("href", "https://github.com/acme/repo/pull/7");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveTextContent("#7 Ship it");
  });

  it("uses explicit fallbacks for missing fields", () => {
    renderCard({ conversation: conversation({ title: null, workspace: null, status: "failed" }) });
    expect(screen.getByText("New session")).toBeInTheDocument();
    expect(screen.getByText("No working directory")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("opens from the keyboard but leaves pointer clicks to selection and drag", () => {
    const { card, onOpen } = renderCard({ conversation: conversation() });
    fireEvent.click(card, { detail: 1 });
    expect(onOpen).not.toHaveBeenCalled();
    fireEvent.keyDown(card, { key: "Enter" });
    fireEvent.click(card, { detail: 0 });
    expect(onOpen).toHaveBeenCalledTimes(2);
    expect(onOpen).toHaveBeenCalledWith("conv_1");
  });
});
