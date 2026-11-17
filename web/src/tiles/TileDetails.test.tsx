import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { TileDetails } from "./TileDetails";

const renameMutate = vi.hoisted(() => vi.fn());

vi.mock("@/hooks/useConversations", () => ({
  useRenameConversation: () => ({ mutate: renameMutate, isPending: false }),
}));
vi.mock("@/hooks/useSession", () => ({ useSession: () => ({ session: null }) }));
vi.mock("@/lib/sessionsApi", () => ({
  getSessionUsage: async () => ({ id: "conv_a", totalCostUsd: 1.5, usageByModel: null }),
}));
vi.mock("@/lib/lastAssistantText", () => ({
  fetchLastAssistantText: async () => "Done: tests pass.",
}));

function renderDetails(overrides: Partial<Conversation> = {}) {
  const conversation = {
    id: "conv_a",
    object: "conversation",
    title: "Fix login",
    created_at: 1,
    updated_at: 2,
    labels: {},
    permission_level: null,
    ...overrides,
  } as Conversation;
  render(
    <QueryClientProvider client={new QueryClient()}>
      <TileDetails id="details" conversation={conversation} title="Fix login" accent="red" />
    </QueryClientProvider>,
  );
}

describe("TileDetails", () => {
  it("renames the session inline", () => {
    renderDetails();
    fireEvent.click(screen.getByRole("button", { name: "Rename Fix login" }));
    const input = screen.getByRole("textbox", { name: "Session name" });
    fireEvent.change(input, { target: { value: "  Fix login redirect " } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(renameMutate).toHaveBeenCalledWith({ id: "conv_a", title: "Fix login redirect" });
  });

  it("shows usage and the latest reply", async () => {
    renderDetails();
    expect(await screen.findByText("$1.50")).toBeInTheDocument();
    expect(await screen.findByText("Done: tests pass.")).toBeInTheDocument();
  });

  it("keeps a sub-agent's title read-only", () => {
    renderDetails({ parent_session_id: "conv_parent" });
    expect(screen.queryByRole("button", { name: /^Rename/ })).not.toBeInTheDocument();
  });
});
