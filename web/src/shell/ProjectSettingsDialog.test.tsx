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
// Hoisted so the vi.mock factory can reference it; per-test overrides let
// cases control the host-resolved model catalog (default: empty, so the
// Claude static-alias fallback is what populates the picker).
const { hostModelOptionsMock } = vi.hoisted(() => ({ hostModelOptionsMock: vi.fn() }));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: [{ host_id: "h1", name: "Laptop", owner: "me", status: "online" }] }),
  useHostModelOptions: hostModelOptionsMock,
}));
// Hoisted so the vi.mock factory below can reference it; per-test overrides
// let cases control the agent catalog (the default is set in beforeEach).
const { availableAgentsMock } = vi.hoisted(() => ({ availableAgentsMock: vi.fn() }));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: availableAgentsMock,
  // The reused agent picker prefetches details on open; no-op in the dialog test.
  prefetchAvailableAgentDetails: vi.fn(),
}));

function pickerAgent(overrides: Record<string, unknown> = {}) {
  return {
    id: "ag_1",
    name: "hello",
    display_name: "Hello",
    description: null,
    harness: null,
    skills: [],
    ...overrides,
  };
}
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
  availableAgentsMock.mockReset();
  availableAgentsMock.mockReturnValue({ data: [pickerAgent()] });
  hostModelOptionsMock.mockReset();
  hostModelOptionsMock.mockReturnValue({ data: [] });
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

  it("preserves the project icon when saving settings", async () => {
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { icon: "🔥", use_worktree: true },
    });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-worktree")).toHaveAttribute(
        "data-state",
        "checked",
      ),
    );

    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { icon: "🔥", use_worktree: true }),
    );
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

  it("shows the base-branch field only when the worktree default is on, and saves it", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    // Base branch is hidden while the worktree default is OFF (nothing to fork).
    expect(screen.queryByTestId("project-settings-base-branch")).not.toBeInTheDocument();

    // Turning the worktree default ON reveals the base-branch field.
    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    const input = screen.getByTestId("project-settings-base-branch");
    fireEvent.change(input, { target: { value: "  main  " } });
    fireEvent.click(screen.getByTestId("project-settings-save"));

    // Trimmed and stored alongside the worktree default.
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { use_worktree: true, base_branch: "main" }),
    );
  });

  it("drops the base branch when the worktree default is off", async () => {
    // A base branch stored from an earlier ON state must not linger as an
    // invisible default once the worktree toggle is turned back off.
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { use_worktree: true, base_branch: "develop" },
    });
    renderDialog();
    // Seeded ON → the base-branch field shows its stored value.
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-base-branch")).toHaveValue("develop"),
    );
    // Turn the worktree default OFF → base branch drops, config clears to {}.
    fireEvent.click(screen.getByTestId("project-settings-worktree"));
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

  it("offers the same agent set as the composer picker (hidden agents excluded)", async () => {
    // Filter parity with the new-session composer (selectableSessionAgents):
    // if this picker offered an agent the composer hides, a project could pin
    // a default the composer then can't show — the silent-substitution setup.
    availableAgentsMock.mockReturnValue({
      data: [
        pickerAgent(),
        pickerAgent({ id: "ag_nessie", name: "nessie", display_name: "Nessie" }),
      ],
    });
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    // Open the agent picker dropdown (Radix opens on pointerdown), then the
    // "Custom agents" submenu where composed agents are listed.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-custom-agents"));
    expect(screen.getByTestId("new-chat-landing-agent-ag_1")).toBeInTheDocument();
    expect(screen.queryByTestId("new-chat-landing-agent-ag_nessie")).not.toBeInTheDocument();
    // And the stored default agent is pinned into discovery, so a
    // session-scoped default that the bounded scan misses still resolves here.
    expect(
      availableAgentsMock.mock.calls.some(
        ([opts]) => (opts as { pinnedAgentIds?: string[] } | undefined)?.pinnedAgentIds != null,
      ),
    ).toBe(true);
  });

  // A model default belongs only to a native harness that takes a model
  // override (Claude Code / Codex). These pin the control's visibility, its
  // round-trip through save, and the data-safety edges around it.
  const claudeAgent = () =>
    pickerAgent({
      id: "ag_claude",
      name: "claude-native-ui",
      display_name: "Claude Code",
      harness: "claude-native",
    });

  it("offers a model default only when the default agent has a model choice", async () => {
    availableAgentsMock.mockReturnValue({ data: [pickerAgent(), claudeAgent()] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude" },
    });
    renderDialog();
    await waitFor(() => expect(screen.getByTestId("project-settings-model")).toBeInTheDocument());

    // Switch the default to a plain bundle agent (under "Custom agents") → the
    // model field goes away.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-custom-agents"));
    fireEvent.click(screen.getByTestId("new-chat-landing-agent-ag_1"));
    await waitFor(() =>
      expect(screen.queryByTestId("project-settings-model")).not.toBeInTheDocument(),
    );
  });

  it("round-trips a stored model default on save", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude", model: "opus" },
    });
    renderDialog();
    await waitFor(() => expect(screen.getByTestId("project-settings-model")).toBeInTheDocument());
    // The stored alias seeds the control (the static Claude vocab labels it).
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-model")).toHaveTextContent(/opus/i),
    );

    // Save untouched — the model rides back out unchanged.
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_claude", model: "opus" }),
    );
  });

  it("drops a stale model default when the agent has no model choice", async () => {
    // A model stored while Claude Code was the default must not linger as an
    // invisible key after the default agent changes to one without a model
    // override (nothing would consume it, and the field can't clear it).
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_1", model: "opus" },
    });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    expect(screen.queryByTestId("project-settings-model")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_1" }));
  });

  it("preserves a stored model when the default agent hasn't resolved from discovery", async () => {
    // While agent discovery is loading/failing, the stored agent's model
    // capability is unknowable — an unrelated save in that window must not
    // silently delete a valid stored default.
    availableAgentsMock.mockReturnValue({ data: [] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude", model: "opus" },
    });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_claude", model: "opus" }),
    );
  });
});
