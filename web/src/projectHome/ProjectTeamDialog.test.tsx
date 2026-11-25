import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import * as projectsApi from "@/lib/projectsApi";
import { ProjectTeamDialog, ROLE_TEMPLATES } from "./ProjectTeamDialog";

vi.mock("@/lib/projectsApi", () => ({
  PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES: 8000,
  createProjectIdentity: vi.fn(async (_projectId: string, input: { name: string }) => ({
    id: "new",
    builtin: false,
    instructions: "",
    ...input,
  })),
  updateProjectIdentity: vi.fn(),
  deleteProjectIdentity: vi.fn(),
}));
vi.mock("./useProjectIdentities", () => ({
  projectIdentitiesKey: (projectId: string) => ["identities", projectId],
  useProjectIdentities: () => ({
    isLoading: false,
    data: [{ id: "lead", name: "Lead", builtin: true, instructions: "" }],
  }),
}));
vi.mock("./useProjectAgentSession", () => ({
  useProjectRecord: () => ({ data: { shared: false, config: {} } }),
}));
vi.mock("./IdentityModelEffortFields", () => ({ IdentityModelEffortFields: () => null }));
vi.mock("./ProjectRoutingSection", () => ({ ProjectRoutingSection: () => null }));

describe("ProjectTeamDialog", () => {
  it("adds a role from a template, in role wording throughout", async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectTeamDialog projectId="project" projectName="Shipping" onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    const nav = screen.getByRole("navigation", { name: "Roles" });
    expect(within(nav).getByRole("button", { name: "New role" })).toBeInTheDocument();
    expect(screen.getByText("Instructions")).toBeInTheDocument();
    expect(screen.getByTestId("project-team-persona")).toHaveValue("");
    const templates = screen.getByRole("group", { name: "Role templates" });
    fireEvent.click(within(templates).getByRole("button", { name: "Designer" }));
    const designer = ROLE_TEMPLATES.find((template) => template.name === "Designer")!;
    expect(screen.getByTestId("project-team-name")).toHaveValue("Designer");
    expect(screen.getByTestId("project-team-persona")).toHaveValue(designer.instructions);
    expect(screen.getByTestId("project-team-owns")).toHaveValue(designer.owns);
    fireEvent.click(screen.getByTestId("project-team-save"));
    await waitFor(() =>
      expect(projectsApi.createProjectIdentity).toHaveBeenCalledWith(
        "project",
        expect.objectContaining({
          name: "Designer",
          instructions: designer.instructions,
          scope: { owns: designer.owns.split("\n"), excludes: designer.excludes.split("\n") },
        }),
      ),
    );
    expect(screen.queryByRole("group", { name: "Role templates" })).toBeNull();
  });
});
