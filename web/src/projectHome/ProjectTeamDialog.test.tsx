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
  listRetiredProjectRoles: vi.fn(async () => retiredRoles.rows),
  restoreProjectIdentity: vi.fn(async (_projectId: string, identityId: string) => ({
    id: identityId,
    name: "zz Designer",
    builtin: false,
    instructions: "",
  })),
}));
const retiredRoles = vi.hoisted(() => ({ rows: [] as Record<string, unknown>[] }));
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
  retiredRoles.rows = [];
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
    const templates = screen.getByRole("group", { name: "Start from a role (optional)" });
    fireEvent.click(within(templates).getByRole("button", { name: "Designer" }));
    const designer = ROLE_TEMPLATES.find((template) => template.name === "Designer")!;
    expect(screen.getByTestId("project-team-name")).toHaveValue("Designer");
    // A new member's name sits in the header, above the templates.
    expect(screen.getByTestId("project-team-identity")).toHaveTextContent("New team member");
    expect(
      screen.getByTestId("project-team-name").compareDocumentPosition(templates) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.getByTestId("project-team-persona")).toHaveValue(designer.instructions);
    // A team member has no Owns / Doesn't own fields.
    expect(screen.queryByText("Owns")).toBeNull();
    expect(screen.queryByText("Doesn't own")).toBeNull();
    fireEvent.click(screen.getByTestId("project-team-save"));
    await waitFor(() =>
      expect(projectsApi.createProjectIdentity).toHaveBeenCalledWith(
        "project",
        expect.objectContaining({ name: "Designer", instructions: designer.instructions }),
      ),
    );
    expect(vi.mocked(projectsApi.createProjectIdentity).mock.calls[0][1]).not.toHaveProperty(
      "scope",
    );
    expect(screen.queryByRole("group", { name: "Start from a role (optional)" })).toBeNull();
  });

  it("lists retired roles for the owner and restores one", async () => {
    retiredRoles.rows = [
      {
        id: "role-1",
        name: "zz Designer",
        retired_at: 1,
        retired_by: "lead",
        reason: "Merged into the Product Designer",
      },
    ];
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectTeamDialog projectId="project" projectName="Shipping" onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    const retired = await screen.findByTestId("project-team-retired");
    expect(retired).toHaveTextContent("zz Designer");
    fireEvent.click(within(retired).getByRole("button", { name: "Restore" }));
    await waitFor(() =>
      expect(projectsApi.restoreProjectIdentity).toHaveBeenCalledWith("project", "role-1"),
    );
  });

  it("starts blank and lets Blank clear a role's prefilled fields", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectTeamDialog projectId="project" projectName="Shipping" onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    const templates = screen.getByRole("group", { name: "Start from a role (optional)" });
    const blank = within(templates).getByRole("button", { name: "Blank" });
    expect(blank).toHaveAttribute("aria-pressed", "true");
    const reviewer = within(templates).getByRole("button", { name: "Reviewer" });
    expect(reviewer).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(reviewer);
    expect(reviewer).toHaveAttribute("aria-pressed", "true");
    expect(blank).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(blank);
    for (const field of ["name", "persona"]) {
      expect(screen.getByTestId(`project-team-${field}`)).toHaveValue("");
    }
    expect(blank).toHaveAttribute("aria-pressed", "true");
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
    // The name is edited in place in the header; no second name row repeats it.
    const header = screen.getByTestId("project-team-identity");
    expect(within(header).getByRole("textbox", { name: "Name" })).toHaveValue("Planner");
    expect(screen.getAllByRole("textbox", { name: "Name" })).toHaveLength(1);
    expect(screen.queryByRole("heading", { name: "Planner" })).toBeNull();
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

  it("saves an edit without touching a role's stored scope", async () => {
    identities.extra = [
      {
        id: "planner",
        name: "Planner",
        builtin: false,
        instructions: "Own scope.",
        scope: { owns: ["plans"], excludes: ["code"] },
      },
    ];
    vi.mocked(projectsApi.updateProjectIdentity).mockResolvedValue({
      id: "planner",
      name: "Planner",
      builtin: false,
      instructions: "Own plans.",
    } as projectsApi.ProjectIdentity);
    render(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient()}>
          <ProjectTeamDialog projectId="project" projectName="Shipping" onClose={vi.fn()} />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    fireEvent.click(
      within(screen.getByRole("navigation", { name: "Team members" })).getByText("Planner"),
    );
    expect(screen.queryByText("plans")).toBeNull();
    fireEvent.change(screen.getByTestId("project-team-persona"), {
      target: { value: "Own plans." },
    });
    fireEvent.click(screen.getByTestId("project-team-save"));
    await waitFor(() => expect(projectsApi.updateProjectIdentity).toHaveBeenCalled());
    const [, , input] = vi.mocked(projectsApi.updateProjectIdentity).mock.calls[0];
    expect(input).toMatchObject({ name: "Planner", instructions: "Own plans." });
    expect(input).not.toHaveProperty("scope");
  });
});
