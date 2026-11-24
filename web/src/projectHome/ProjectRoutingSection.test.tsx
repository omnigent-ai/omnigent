import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ProjectsApi from "@/lib/projectsApi";
import type { Project, ProjectIdentity } from "@/lib/projectsApi";
import { ProjectRoutingSection } from "./ProjectRoutingSection";

const mocks = vi.hoisted(() => ({
  projects: [] as Project[],
  setProjectRouting: vi.fn(),
}));

vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => ({ data: mocks.projects }),
}));

vi.mock("@/lib/projectsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ProjectsApi>()),
  setProjectRouting: mocks.setProjectRouting,
}));

const PM = { id: "pm", name: "PM", builtin: false } as ProjectIdentity;

function renderSection(project: Partial<Project>, personas: ProjectIdentity[]) {
  mocks.projects = [{ id: "p1", name: "P", ...project }];
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ProjectRoutingSection projectId="p1" personas={personas} />
    </QueryClientProvider>,
  );
}

describe("ProjectRoutingSection", () => {
  beforeEach(() => mocks.setProjectRouting.mockReset().mockResolvedValue({}));

  it("removes the Lead, dropping an explicit Lead choice", async () => {
    renderSection(
      { router_identity_id: "orchestrator", router: { identity_id: "orchestrator", name: "Lead" } },
      [PM],
    );
    expect(screen.getByText(/Now: Lead\./)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Remove the Lead" }));
    await waitFor(() =>
      expect(mocks.setProjectRouting).toHaveBeenCalledWith("p1", {
        router_identity_id: null,
        lead_removed: true,
      }),
    );
  });

  it("keeps the Lead while no other persona can route", () => {
    renderSection({}, []);
    expect(screen.getByRole("button", { name: "Remove the Lead" })).toBeDisabled();
  });

  it("restores a removed Lead and keeps the chosen router", async () => {
    renderSection({ lead_removed: true, router_identity_id: "pm" }, [PM]);
    fireEvent.click(screen.getByRole("button", { name: "Restore the Lead" }));
    await waitFor(() =>
      expect(mocks.setProjectRouting).toHaveBeenCalledWith("p1", {
        router_identity_id: "pm",
        lead_removed: false,
      }),
    );
  });

  it("is hidden from members of a shared project", () => {
    renderSection({ shared: true }, [PM]);
    expect(screen.queryByTestId("project-routing")).toBeNull();
  });
});
