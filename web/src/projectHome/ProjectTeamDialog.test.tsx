import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
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
const identities = vi.hoisted(() => ({
  extra: [] as Record<string, unknown>[],
}));
vi.mock("./useProjectIdentities", () => ({
  projectIdentitiesKey: (projectId: string) => ["identities", projectId],
  useProjectIdentities: () => ({
    isLoading: false,
    data: [{ id: "lead", name: "Lead", builtin: true, instructions: "" }, ...identities.extra],
  }),
}));
vi.mock("./useProjectAgentSession", () => ({
  useProjectRecord: () => ({ data: { shared: false, config: {} } }),
}));
vi.mock("./IdentityModelEffortFields", () => ({
  IdentityModelEffortFields: () => <div data-testid="runtime-fields" />,
}));
vi.mock("./ProjectRoutingSection", () => ({
  ProjectRoutingSection: () => <div data-testid="routing-controls" />,
}));

afterEach(() => {
  identities.extra = [];
});

describe("ProjectTeamDialog", () => {
  it("adds a team member from a template, in team-member wording throughout", async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectTeamDialog projectId="project" projectName="Shipping" onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    const nav = screen.getByRole("navigation", { name: "Team members" });
    expect(within(nav).getByRole("button", { name: "New team member" })).toBeInTheDocument();
    expect(screen.getByText("Instructions")).toBeInTheDocument();
    expect(screen.getByTestId("project-team-persona")).toHaveValue("");
    const templates = screen.getByRole("group", { name: "Start with a role" });
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
    expect(screen.queryByRole("group", { name: "Start with a role" })).toBeNull();
  });

  it("says a team member runs its own copy, and links the owner to its tools", () => {
    identities.extra = [
      {
        id: "pe",
        name: "Platform Engineer",
        builtin: false,
        instructions: "Own the platform.",
        project_agent_id: "agent-copy",
      },
    ];
    render(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient()}>
          <ProjectTeamDialog projectId="project" projectName="Shipping" onClose={vi.fn()} />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    const nav = screen.getByRole("navigation", { name: "Team members" });
    fireEvent.click(within(nav).getByText("Platform Engineer"));
    expect(screen.getByTestId("project-team-own-copy")).toHaveTextContent(
      "This team member gets its own copy",
    );
    expect(
      within(screen.getByTestId("project-team-tools")).getByRole("link", {
        name: "Edit Platform Engineer's tools and permissions",
      }),
    ).toHaveAttribute("href", "/settings/custom-agents/agent-copy");
  });

  it("keeps agent and model collapsed and routing in its own section", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectTeamDialog projectId="project" projectName="Shipping" onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId("project-team-bytes")).toBeNull();
    fireEvent.change(screen.getByTestId("project-team-persona"), {
      target: { value: "x".repeat(6400) },
    });
    expect(screen.getByTestId("project-team-bytes")).toHaveTextContent("6,400 / 8,000 bytes");
    const toggle = screen.getByTestId("project-team-runtime-toggle");
    expect(toggle).toHaveTextContent("Project defaults");
    expect(screen.queryByTestId("runtime-fields")).toBeNull();
    fireEvent.click(toggle);
    expect(screen.getByTestId("runtime-fields")).toBeInTheDocument();
    expect(screen.queryByTestId("routing-controls")).toBeNull();
    fireEvent.click(screen.getByTestId("project-team-routing"));
    expect(screen.getByRole("heading", { name: "Routing" })).toBeInTheDocument();
    expect(screen.getByTestId("routing-controls")).toBeInTheDocument();
    expect(screen.queryByTestId("project-team-save")).toBeNull();
    const nav = screen.getByRole("navigation", { name: "Team members" });
    fireEvent.click(within(nav).getByRole("button", { name: "New team member" }));
    expect(screen.getByTestId("project-team-save")).toHaveTextContent("Add member");
  });

  it("heads the Lead with its identity instead of a disabled name field", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectTeamDialog projectId="project" projectName="Shipping" onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByTestId("project-team-lead"));
    expect(screen.getByRole("heading", { name: "Lead" })).toBeInTheDocument();
    expect(screen.getByTestId("project-team-identity")).toHaveTextContent("Built-in coordinator");
    expect(screen.queryByTestId("project-team-name")).toBeNull();
    expect(screen.getByText("Charter")).toBeInTheDocument();
    expect(screen.getByTestId("project-team-lead-note")).toHaveTextContent(
      "Only you can edit the charter; agents can't.",
    );
    expect(screen.queryByTestId("project-team-bytes")).toBeNull();
  });

  it("keeps a custom role renamable and shows the byte count only near the limit", () => {
    identities.extra = [
      { id: "planner", name: "Planner", builtin: false, instructions: "Own scope." },
    ];
    render(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient()}>
          <ProjectTeamDialog projectId="project" projectName="Shipping" onClose={vi.fn()} />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    const nav = screen.getByRole("navigation", { name: "Team members" });
    fireEvent.click(within(nav).getByText("Planner"));
    expect(screen.getByRole("heading", { name: "Planner" })).toBeInTheDocument();
    expect(screen.getByTestId("project-team-name")).toBeEnabled();
    expect(screen.getByTestId("project-team-timing")).toHaveTextContent(
      "Changes apply from Planner's next session.",
    );
    expect(screen.queryByTestId("project-team-bytes")).toBeNull();
    const persona = screen.getByTestId("project-team-persona");
    fireEvent.change(persona, { target: { value: "x".repeat(6400) } });
    expect(screen.getByTestId("project-team-bytes")).toHaveTextContent("6,400 / 8,000 bytes");
    expect(screen.getByTestId("project-team-save")).toBeEnabled();
    fireEvent.change(persona, { target: { value: "x".repeat(8001) } });
    expect(screen.getByTestId("project-team-bytes")).toHaveTextContent(
      "8,001 / 8,000 bytes. Shorten it to save.",
    );
    expect(persona).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByTestId("project-team-save")).toBeDisabled();
  });
});
