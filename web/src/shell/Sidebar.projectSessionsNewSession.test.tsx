import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import { ProjectNewSessionButton, SidebarRowGroups } from "./Sidebar";

afterEach(cleanup);

const row = (id: string) => ({ id, title: id }) as unknown as Conversation;

function renderGroups(rows: Conversation[]) {
  render(
    <MemoryRouter>
      <TooltipProvider>
        <SidebarRowGroups
          rows={rows}
          agentRow={(conversation) => conversation.id.startsWith("agent")}
          footer={null}
          renderList={(groupRows) => (
            <ul>
              {groupRows.map((conversation) => (
                <li key={conversation.id}>{conversation.id}</li>
              ))}
            </ul>
          )}
          sessionsAction={<ProjectNewSessionButton projectName="Ship It" onNavigate={vi.fn()} />}
        />
      </TooltipProvider>
    </MemoryRouter>,
  );
}

describe("a project's Sessions folder", () => {
  it("offers a new session in the project on the Sessions header only", () => {
    renderGroups([row("agent-lead"), row("chat-1")]);
    const sessions = screen.getByTestId("sidebar-row-group-sessions");
    const button = within(sessions).getByRole("link", { name: "New session in Ship It" });
    expect(button.getAttribute("href")).toBe("/?project=Ship%20It");
    expect(within(screen.getByTestId("sidebar-row-group-agents")).queryByRole("link")).toBeNull();
  });

  it("adds nothing when the project has no separate folders", () => {
    renderGroups([row("chat-1"), row("chat-2")]);
    expect(screen.queryByRole("link", { name: "New session in Ship It" })).toBeNull();
  });
});
