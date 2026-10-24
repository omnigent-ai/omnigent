import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useChildSessions, type ChildSessionInfo } from "@/hooks/useChildSessions";
import { SidebarChildSessions } from "./SidebarChildSessions";

vi.mock("@/hooks/useChildSessions", () => ({ useChildSessions: vi.fn() }));
afterEach(cleanup);

function renderChildren(children: ChildSessionInfo[]) {
  vi.mocked(useChildSessions).mockReturnValue({ children, isLoading: false, error: null });
  render(
    <MemoryRouter initialEntries={["/c/parent?panel=agents&o=workspace"]}>
      <ul>
        <SidebarChildSessions conversationId="parent" />
      </ul>
    </MemoryRouter>,
  );
}

describe("sidebar child chats", () => {
  it("shows each child with its status and a link that preserves workspace context", () => {
    renderChildren([
      { id: "images", title: "Image attachments", busy: true, pending_elicitations_count: 0 },
      { id: "panes", title: "Split panes", busy: false, pending_elicitations_count: 1 },
    ] as ChildSessionInfo[]);
    expect(screen.getByText("2 child chats")).toBeVisible();
    expect(screen.getByRole("link", { name: "Image attachments: Working" })).toHaveAttribute(
      "href",
      "/c/images?o=workspace",
    );
    expect(screen.getByRole("link", { name: "Split panes: Needs response" })).toHaveAttribute(
      "href",
      "/c/panes?o=workspace",
    );
  });

  it("does not show an empty section or closed children", () => {
    renderChildren([
      {
        id: "closed",
        title: null,
        task_summary: null,
        tool: null,
        session_name: null,
        current_task_status: null,
        busy: false,
        last_message_preview: null,
        pending_elicitations_count: 0,
        labels: { "omnigent.closed": "true" },
      },
    ]);
    expect(screen.queryByTestId("sidebar-child-sessions")).toBeNull();
  });
});
