import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ProjectSettingsDialog } from "./ProjectSettingsDialog";
import { getProject, updateProjectConfig, createProject } from "@/lib/projectsApi";

vi.mock("@/lib/projectsApi", () => ({
  getProject: vi.fn(),
  updateProjectConfig: vi.fn(),
  createProject: vi.fn(),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: [{ host_id: "h1", name: "Laptop", owner: "me", status: "online" }] }),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [
      {
        id: "ag_1",
        name: "hello",
        display_name: "Hello",
        description: null,
        harness: null,
        skills: [],
      },
    ],
  }),
  // The reused agent picker prefetches details on open; no-op in the dialog test.
  prefetchAvailableAgentDetails: vi.fn(),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ managed_sandboxes_enabled: false, sandbox_provider: null }),
}));
// The filesystem browser owns its own data-fetching; stub it to a marker plus
// a button that reports a navigated path, so we can drive the disclosure and
// the live workspace update without the host-filesystem plumbing.
vi.mock("./WorkspacePicker", () => ({
  isNavigablePath: (p: string) => p.startsWith("/"),
  WorkspacePicker: ({ onNavigate }: { onNavigate: (p: string) => void }) => (
    <div data-testid="mock-workspace-picker">
      <button type="button" onClick={() => onNavigate("/picked/dir")}>
        pick dir
      </button>
    </div>
  ),
}));

const getProjectMock = vi.mocked(getProject);
const updateMock = vi.mocked(updateProjectConfig);
const createMock = vi.mocked(createProject);

function renderDialog(projectId: string | null = "p_1") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ProjectSettingsDialog open onOpenChange={vi.fn()} projectId={projectId} projectName="Work" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getProjectMock.mockReset();
  updateMock.mockReset();
  createMock.mockReset();
  updateMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
});

afterEach(cleanup);

describe("ProjectSettingsDialog", () => {
  it("seeds fields from the project's stored config", async () => {
    // No stored host → the working directory can't be chosen yet, so it shows
    // the "pick a host first" placeholder rather than a path input / browser.
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { use_worktree: true },
    });
    renderDialog();
    // use_worktree:true was stored → toggle seeds ON once the fetch settles.
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-worktree")).toHaveAttribute(
        "data-state",
        "checked",
      ),
    );
    // No stored host → the working directory can't be chosen yet, so it shows
    // the "pick a host first" placeholder rather than a path input / browser.
    expect(screen.getByTestId("project-settings-workspace")).toHaveTextContent(
      /pick a host first/i,
    );
  });

  it("saves only the fields that are set (unset slots omitted)", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    // Wait for the config fetch to settle (Save enabled) so the seeding effect
    // has run before we interact — otherwise the seed would clobber our change.
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    // Turn the worktree default ON — the only field touched.
    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => expect(updateMock).toHaveBeenCalled());
    expect(updateMock).toHaveBeenCalledWith("p_1", { use_worktree: true });
  });

  it("stores nothing for the worktree toggle when left at its default (OFF)", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    // Leave the toggle OFF (default) → config clears to {} (use_worktree absent).
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("p_1", {}));
  });

  it("hides the sandbox option when managed sandboxes are disabled", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    // The online host is offered, but no "Sandbox" option (managed sandboxes off).
    expect(screen.getAllByText("Laptop").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Sandbox/)).not.toBeInTheDocument();
  });

  it("promotes a label-only folder (id=null) on save via createProject", async () => {
    createMock.mockResolvedValue({ id: "p_new", name: "Work" });
    renderDialog(null);
    // No fetch for a label-only folder (nothing to read).
    expect(getProjectMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => expect(createMock).toHaveBeenCalledWith("Work"));
    expect(updateMock).toHaveBeenCalledWith("p_new", { use_worktree: true });
  });

  it("persists host, workspace, and agent from a seeded config on save", async () => {
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { host_id: "h1", workspace: "/repo", agent_id: "ag_1" },
    });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    // Save without touching anything — the seeded fields round-trip back out.
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        host_id: "h1",
        workspace: "/repo",
        agent_id: "ag_1",
      }),
    );
  });

  it("opens the working-directory browser, updates the path, then closes on outside click", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    renderDialog();
    // A stored online host makes the working directory browsable — a compact
    // "Browse…" trigger, collapsed by default (no picker mounted yet).
    await waitFor(() => expect(screen.getByText("Browse…")).toBeInTheDocument());
    expect(screen.queryByTestId("mock-workspace-picker")).not.toBeInTheDocument();

    // Expand → the browser mounts; navigating updates the trigger label live.
    fireEvent.click(screen.getByText("Browse…"));
    expect(screen.getByTestId("mock-workspace-picker")).toBeInTheDocument();
    fireEvent.click(screen.getByText("pick dir"));
    expect(screen.getByText("/picked/dir")).toBeInTheDocument();

    // The click-away backdrop closes the browser, keeping the picked path.
    fireEvent.click(screen.getByRole("button", { name: /close directory browser/i }));
    expect(screen.queryByTestId("mock-workspace-picker")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { host_id: "h1", workspace: "/picked/dir" }),
    );
  });

  it("blocks Save (does not clear defaults) when the config load fails", async () => {
    // A transient GET failure must NOT be read as "no config" — otherwise
    // saving the blank draft would send `{}` and wipe the stored defaults.
    getProjectMock.mockRejectedValue(new Error("500 Server Error"));
    renderDialog();

    // The load-error notice shows and Save stays disabled.
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-load-error")).toBeInTheDocument(),
    );
    expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(true);

    // Even if a submit is forced, onSubmit bails — no clearing PATCH is sent.
    fireEvent.submit(screen.getByTestId("project-settings-save").closest("form")!);
    expect(updateMock).not.toHaveBeenCalled();
  });
});
