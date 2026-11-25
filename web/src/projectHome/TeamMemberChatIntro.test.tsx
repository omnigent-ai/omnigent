import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatPresentation } from "@/components/chat/assistantPresentation";
import { getSessionDraft, setSessionDraft } from "@/lib/sessionDrafts";
import { ProjectPersonaChat, teamMemberCharter } from "./ProjectOrchestrator";

const transcript = vi.hoisted(() => ({ empty: true }));
vi.mock("@/components/chat/FullSessionChat", () => ({
  FullSessionChat: ({ presentation }: { presentation?: ChatPresentation }) => (
    <div>{presentation?.header?.(transcript.empty)}</div>
  ),
}));
vi.mock("@/hooks/useAgents", () => ({ useSessionAgent: () => ({ data: undefined }) }));
// The host-update banner reads /health through React Query; it isn't what these tests check.
vi.mock("./OutdatedHostBanner", () => ({ OutdatedHostBanner: () => null }));

afterEach(() => {
  cleanup();
  transcript.empty = true;
});

function renderChat() {
  render(
    <MemoryRouter>
      <ProjectPersonaChat
        sessionId="designer-session"
        name="Designer"
        charter={teamMemberCharter(
          "You are the product designer. Own layouts and wording.\nKeep screens consistent.",
        )}
        onClose={vi.fn()}
      />
    </MemoryRouter>,
  );
}

describe("a team member's new chat", () => {
  it("shows its charter and fills the composer from a starter prompt", () => {
    setSessionDraft("designer-session", { text: "", files: [] });
    renderChat();
    expect(screen.getByTestId("team-member-charter")).toHaveTextContent(
      "You are the product designer. Own layouts and wording.",
    );
    const starters = screen.getByRole("group", { name: "Ways to start with Designer" });
    expect(within(starters).getAllByRole("button")).toHaveLength(3);
    fireEvent.click(within(starters).getByRole("button", { name: "What are you working on?" }));
    expect(getSessionDraft("designer-session")?.text).toBe("What are you working on right now?");
  });

  it("stays out of the way once the chat has messages", () => {
    transcript.empty = false;
    renderChat();
    expect(screen.queryByTestId("team-member-charter")).toBeNull();
    expect(screen.queryByRole("group", { name: "Ways to start with Designer" })).toBeNull();
  });

  it("takes the opening of the instructions as the charter", () => {
    expect(teamMemberCharter("  Own the roadmap!  Then write tickets.")).toBe(
      "Own the roadmap! Then write tickets.",
    );
    expect(teamMemberCharter("No full stop here")).toBe("No full stop here");
    expect(teamMemberCharter("You are the PM. Own scope. Write tickets.")).toBe(
      "You are the PM. Own scope.",
    );
    expect(teamMemberCharter("")).toBeNull();
    expect(teamMemberCharter(`${"a".repeat(300)}.`)?.length).toBe(200);
  });
});
