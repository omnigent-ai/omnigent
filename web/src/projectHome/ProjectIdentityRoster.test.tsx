import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { ProjectIdentity } from "@/lib/projectsApi";
import { ProjectIdentityRoster, VISIBLE_ROSTER_LIMIT } from "./ProjectIdentityRoster";

const names = ["Reviewer", "Comms", "Merger", "Designer", "QA Lead", "Docs Writer"];
const identities: ProjectIdentity[] = names.map((name, index) => ({
  id: `id-${index}`,
  project_id: "p1",
  name,
  builtin: false,
  instructions: "",
  agent_id: null,
  model: null,
  effort: null,
  active_session_id: null,
  // The last persona is working, so it is kept in view.
  active_session_state: index === names.length - 1 ? "busy" : "idle",
}));

vi.mock("./useProjectIdentities", () => ({
  projectIdentitiesKey: (projectId: string | null) => ["project-identities", projectId],
  useProjectIdentities: () => ({ data: identities }),
}));

describe("ProjectIdentityRoster", () => {
  it("shows at most the limit in the header and the rest in a +N menu", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <TooltipProvider>
            <ProjectIdentityRoster projectId="p1" projectName="P" />
          </TooltipProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const shown = screen.getAllByTestId("project-identity-button");
    expect(shown).toHaveLength(VISIBLE_ROSTER_LIMIT);
    expect(shown[0]).toHaveAccessibleName("Talk to Docs Writer (working)");
    const more = screen.getByRole("button", { name: "2 more personas" });
    expect(more).toHaveTextContent("+2");
    fireEvent.pointerDown(more, { button: 0, ctrlKey: false });
    const menu = screen.getByRole("menu");
    const items = within(menu).getAllByRole("menuitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("Designer");
    expect(items[1]).toHaveTextContent("QA Lead");
  });
});

describe("ProjectIdentityRoster on phones", () => {
  it("folds the Lead, every persona and Edit team into one Team menu", async () => {
    const viewport = await import("@/hooks/useIsMobileViewport");
    const mobile = vi.spyOn(viewport, "useIsMobileViewport").mockReturnValue(true);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <TooltipProvider>
            <ProjectIdentityRoster
              projectId="p1"
              projectName="P"
              leadPath="/projects/P/orchestrator"
            />
          </TooltipProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.queryAllByTestId("project-identity-button")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Edit team" })).toBeNull();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Team" }), {
      button: 0,
      ctrlKey: false,
    });
    const items = within(screen.getByRole("menu")).getAllByRole("menuitem");
    // The Lead, every persona, then Edit team.
    expect(items).toHaveLength(names.length + 2);
    expect(items[0]).toHaveTextContent("Lead");
    expect(items.at(-1)).toHaveTextContent("Edit team");
    mobile.mockRestore();
  });
});
