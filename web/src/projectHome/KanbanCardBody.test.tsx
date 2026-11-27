import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import type * as LastAssistantText from "@/lib/lastAssistantText";
import { KanbanCardBody } from "./ProjectKanbanBoard";
import { kanbanColumns } from "./projectKanban";

vi.mock("@/lib/lastAssistantText", async (importOriginal) => ({
  ...(await importOriginal<typeof LastAssistantText>()),
  fetchLastAssistantMessage: vi.fn(async () => null),
}));

afterEach(cleanup);

const family = { id: "family", representative_id: "head", pinned: false };
const session = (id: string, title: string, fields: Partial<Conversation>) =>
  ({
    id,
    title,
    created_at: 1,
    family,
    labels: { "omnigent.last_task_error_at": String(fields.updated_at) },
    ...fields,
  }) as Conversation;
const columnOf = (c: Conversation) => (c.status === "failed" ? "Needs attention" : "Done");

function renderCard(...members: Conversation[]) {
  const card = [...kanbanColumns(members, columnOf).values()].flat()[0];
  render(
    <QueryClientProvider client={new QueryClient()}>
      <KanbanCardBody card={card} />
    </QueryClientProvider>,
  );
  return card;
}

describe("KanbanCardBody", () => {
  it("names the nested session that put the card in Needs attention", () => {
    const card = renderCard(
      session("head", "Plan the rollout", { status: "idle", updated_at: 100 }),
      session("nested", "Worker that failed to start", { status: "failed", updated_at: 200 }),
    );
    expect(card.column).toBe("Needs attention");
    expect(screen.getByTestId("kanban-card-nested")).toHaveTextContent("1 nested");
    const note = screen.getByTestId("kanban-card-nested-attention");
    expect(note).toHaveTextContent("1 failed");
    expect(note.querySelector("[title]")).toHaveAttribute("title", "Worker that failed to start");
  });

  it("shows no note once the failure is older than the family's latest activity", () => {
    const card = renderCard(
      session("head", "Draft the summary", { status: "idle", updated_at: 200 }),
      session("nested", "Earlier worker that failed", { status: "failed", updated_at: 100 }),
    );
    expect(card.column).toBe("Done");
    expect(screen.getByTestId("kanban-card-nested")).toHaveTextContent("1 nested");
    expect(screen.queryByTestId("kanban-card-nested-attention")).toBeNull();
  });
});
