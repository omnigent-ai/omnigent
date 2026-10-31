import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ProjectSettingsDialog } from "./ProjectSettingsDialog";
import { getProject, updateProjectConfig, createProject } from "@/lib/projectsApi";
import {
  readProjectLaunchDefaults,
  writeProjectLaunchDefaults,
} from "@/lib/projectLaunchPreferences";
import { TooltipProvider } from "@/components/ui/tooltip";

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
    harness: "claude-sdk",
    skills: [],
    ...overrides,
  };
}
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ managed_sandboxes_enabled: false, sandbox_provider: null }),
}));
// The filesystem browser owns its own data-fetching; stub its explicit commit
// action so this suite can drive the shared dialog without filesystem plumbing.
vi.mock("./WorkspacePicker", () => ({
  isNavigablePath: (p: string) => p.startsWith("/"),
  WorkspacePicker: ({ onSelect }: { onSelect: (p: string) => void }) => (
    <div data-testid="mock-workspace-picker">
      <button type="button" onClick={() => onSelect("/picked/dir")}>
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
      <TooltipProvider>
        <ProjectSettingsDialog
          open
          onOpenChange={vi.fn()}
          projectId={projectId}
          projectName="Work"
        />
      </TooltipProvider>
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

afterEach(() => {
  cleanup();
  localStorage.clear();
});

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

  it("opens the working-directory dialog and commits the confirmed path", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    renderDialog();
    // A stored online host makes the working directory browsable — a compact
    // "Browse…" trigger, collapsed by default (no picker mounted yet).
    await waitFor(() => expect(screen.getByText("Browse…")).toBeInTheDocument());
    expect(screen.queryByTestId("mock-workspace-picker")).not.toBeInTheDocument();

    // Open the shared modal; confirming updates the trigger and closes it.
    fireEvent.click(screen.getByText("Browse…"));
    expect(screen.getByTestId("mock-workspace-picker")).toBeInTheDocument();
    fireEvent.click(screen.getByText("pick dir"));
    expect(screen.getByText("/picked/dir")).toBeInTheDocument();
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
    // custom-agent "Other..." submenu where composed agents are listed.
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

    // Switch the default to a plain bundle agent (under "Other...") → the
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
      config: { agent_id: "ag_claude", model: "opus", effort: "high" },
    });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        agent_id: "ag_claude",
        model: "opus",
        effort: "high",
      }),
    );
  });

  // Effort default: offered beside the model for harnesses with an effort
  // ladder, stored as `effort`, and dropped when it no longer applies.
  async function pickSelectOption(testId: string, option: string | RegExp): Promise<void> {
    fireEvent.pointerDown(screen.getByTestId(testId), { button: 0, pointerType: "mouse" });
    fireEvent.click(await screen.findByRole("option", { name: option }));
  }

  const codexAgent = () =>
    pickerAgent({
      id: "ag_codex",
      name: "codex-native-ui",
      display_name: "Codex",
      harness: "codex-native",
    });
  const ladder = (...efforts: string[]) => efforts.map((reasoningEffort) => ({ reasoningEffort }));

  it("stores a picked effort for Claude Code and hides the field for an agent without one", async () => {
    availableAgentsMock.mockReturnValue({ data: [pickerAgent(), claudeAgent()] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude" },
    });
    renderDialog();
    await waitFor(() => expect(screen.getByTestId("project-settings-effort")).toBeInTheDocument());

    await pickSelectOption("project-settings-effort", "High");
    expect(screen.getByTestId("project-settings-effort")).toHaveTextContent("High");
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_claude", effort: "high" }),
    );

    // A bundle agent has no create-time effort → no field.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-custom-agents"));
    fireEvent.click(screen.getByTestId("new-chat-landing-agent-ag_1"));
    await waitFor(() =>
      expect(screen.queryByTestId("project-settings-effort")).not.toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenLastCalledWith("p_1", { agent_id: "ag_1" }));
  });

  it("treats a malformed stored effort as no default", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude", effort: 3 as unknown as string },
    });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-effort")).toHaveTextContent("No default"),
    );
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_claude" }));
  });

  it("offers Pi's own effort ladder and stores a Pi-only level", async () => {
    const piAgent = pickerAgent({
      id: "ag_pi",
      name: "pi-native-ui",
      display_name: "Pi",
      harness: "pi-native",
    });
    availableAgentsMock.mockReturnValue({ data: [piAgent] });
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { agent_id: "ag_pi" } });
    renderDialog();
    await waitFor(() => expect(screen.getByTestId("project-settings-effort")).toBeInTheDocument());

    await pickSelectOption("project-settings-effort", "Minimal");
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_pi", effort: "minimal" }),
    );
  });

  it("drops a stored Codex effort the loaded ladder lacks, even after a model change", async () => {
    availableAgentsMock.mockReturnValue({ data: [codexAgent()] });
    hostModelOptionsMock.mockReturnValue({
      data: [
        {
          id: "gpt-5.5",
          displayName: "GPT-5.5",
          supportedReasoningEfforts: ladder("low", "xhigh"),
        },
        { id: "gpt-mini", displayName: "GPT mini", supportedReasoningEfforts: ladder("low") },
      ],
    });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_codex", model: "gpt-mini", effort: "xhigh" },
    });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-model")).toHaveTextContent("GPT mini"),
    );
    expect(screen.getByTestId("project-settings-effort")).toHaveTextContent("No default");
    expect(screen.getByText(/saved effort \(xHigh\) isn't available/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenLastCalledWith("p_1", {
        agent_id: "ag_codex",
        model: "gpt-mini",
      }),
    );

    // Switching to a model that has xhigh must not bring the hidden level back.
    await pickSelectOption("project-settings-model", "GPT-5.5");
    expect(screen.getByTestId("project-settings-effort")).toHaveTextContent("No default");
    expect(screen.queryByText(/saved effort/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenLastCalledWith("p_1", {
        agent_id: "ag_codex",
        model: "gpt-5.5",
      }),
    );
  });

  it.each([
    {
      label: "is cleared",
      change: () => fireEvent.click(screen.getByRole("button", { name: "Clear" })),
      saved: {},
    },
    {
      label: "changes",
      change: () => {
        fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
        fireEvent.click(screen.getByTestId("new-chat-landing-agent-ag_codex"));
      },
      saved: { agent_id: "ag_codex" },
    },
  ])("drops the effort default when the default agent $label", async ({ change, saved }) => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent(), codexAgent()] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude", effort: "max" },
    });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-effort")).toHaveTextContent("Max"),
    );
    change();
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("p_1", saved));
  });

  it("offers the Codex model's effort ladder and drops an effort the new model lacks", async () => {
    availableAgentsMock.mockReturnValue({ data: [codexAgent()] });
    hostModelOptionsMock.mockReturnValue({
      data: [
        {
          id: "gpt-5.5",
          displayName: "GPT-5.5",
          isDefault: true,
          supportedReasoningEfforts: ladder("low", "medium", "high", "xhigh"),
        },
        { id: "gpt-mini", displayName: "GPT mini", supportedReasoningEfforts: ladder("low") },
      ],
    });
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { agent_id: "ag_codex" } });
    renderDialog();
    await waitFor(() => expect(screen.getByTestId("project-settings-effort")).toBeInTheDocument());

    // No model pinned → the catalog default's ladder.
    await pickSelectOption("project-settings-effort", "xHigh");
    // A model whose ladder lacks xhigh resets the effort to no default.
    await pickSelectOption("project-settings-model", "GPT mini");
    expect(screen.getByTestId("project-settings-effort")).toHaveTextContent("No default");
    fireEvent.pointerDown(screen.getByTestId("project-settings-effort"), {
      button: 0,
      pointerType: "mouse",
    });
    expect(screen.queryByRole("option", { name: "xHigh" })).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("option", { name: "Low" }));

    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        agent_id: "ag_codex",
        model: "gpt-mini",
        effort: "low",
      }),
    );
  });

  it.each([
    { label: "the catalog is unavailable, plain save", catalog: [], clearModel: false },
    { label: "the catalog is unavailable, model cleared", catalog: [], clearModel: true },
    {
      label: "the catalog lists neither model nor a default, model cleared",
      catalog: [
        { id: "gpt-mini", displayName: "GPT mini", supportedReasoningEfforts: ladder("low") },
      ],
      clearModel: true,
    },
  ])(
    "keeps a stored Codex effort while its ladder is unknown ($label)",
    async ({ catalog, clearModel }) => {
      availableAgentsMock.mockReturnValue({ data: [codexAgent()] });
      hostModelOptionsMock.mockReturnValue({ data: catalog });
      getProjectMock.mockResolvedValue({
        id: "p_1",
        name: "Work",
        config: { agent_id: "ag_codex", model: "gpt-5.5", effort: "high" },
      });
      renderDialog();
      await waitFor(() =>
        expect(screen.getByTestId("project-settings-effort")).toHaveTextContent("High"),
      );
      if (clearModel) await pickSelectOption("project-settings-model", "No default");
      expect(screen.getByTestId("project-settings-effort")).toHaveTextContent("High");
      fireEvent.click(screen.getByTestId("project-settings-save"));
      await waitFor(() =>
        expect(updateMock).toHaveBeenCalledWith("p_1", {
          agent_id: "ag_codex",
          ...(clearModel ? {} : { model: "gpt-5.5" }),
          effort: "high",
        }),
      );
    },
  );

  // The project's last start (remembered in this browser) fills the fields, so
  // the dialog shows what the project's next session opens with.
  it("fills the fields from the project's last start and saves them as the settings", async () => {
    availableAgentsMock.mockReturnValue({ data: [pickerAgent(), claudeAgent()] });
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { agent_id: "ag_1" } });
    writeProjectLaunchDefaults(
      "p_1",
      {
        agentId: "ag_claude",
        pickedHarness: null,
        harness: "claude-native",
        harnessOptions: { model: "opus", effort: "high", mode: "default" },
        hostId: "h1",
        workspace: "/repo-b",
      },
      { agent_id: "ag_1" },
    );
    renderDialog();

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-model")).toHaveTextContent(/opus/i),
    );
    expect(screen.getByTestId("project-settings-last-start")).toBeInTheDocument();
    expect(screen.getByTestId("project-settings-effort")).toHaveTextContent("High");
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        agent_id: "ag_claude",
        host_id: "h1",
        workspace: "/repo-b",
        model: "opus",
        effort: "high",
      }),
    );
  });

  it("keeps the stored agent when the last start's agent no longer exists", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { agent_id: "ag_1" } });
    writeProjectLaunchDefaults("p_1", { agentId: "ag_gone", hostId: "h1" }, { agent_id: "ag_1" });
    renderDialog();

    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_1", host_id: "h1" }),
    );
  });

  it("replaces the last start even when the saved settings match the stored ones", async () => {
    availableAgentsMock.mockReturnValue({ data: [pickerAgent(), claudeAgent()] });
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { agent_id: "ag_1" } });
    writeProjectLaunchDefaults(
      "p_1",
      { agentId: "ag_claude", pickedHarness: null },
      { agent_id: "ag_1" },
    );
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-last-start")).toBeInTheDocument(),
    );

    // Restore the stored agent, so the saved settings match the stored ones.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-custom-agents"));
    fireEvent.click(screen.getByTestId("new-chat-landing-agent-ag_1"));
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_1" }));
    await waitFor(() => expect(readProjectLaunchDefaults("p_1", { agent_id: "ag_1" })).toBeNull());
  });

  it("saves a last-start agent still resolving with its own model and effort", async () => {
    // Discovery so far holds only catalog rows, without the remembered agent.
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()], isPlaceholderData: true });
    const stored = { agent_id: "ag_claude", model: "opus", effort: "low" };
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: stored });
    writeProjectLaunchDefaults(
      "p_1",
      {
        agentId: "ag_custom",
        harness: "claude-native",
        harnessOptions: { model: "sonnet", effort: "high" },
      },
      stored,
    );
    renderDialog();

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-last-start")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        agent_id: "ag_custom",
        model: "sonnet",
        effort: "high",
      }),
    );
  });

  it("leaves the last start in place when the filled-in dialog is cancelled", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    const launch = { agentId: "ag_1", hostId: "h1", workspace: "/repo-b" };
    writeProjectLaunchDefaults("p_1", launch, {});
    renderDialog();

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-last-start")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(updateMock).not.toHaveBeenCalled();
    expect(readProjectLaunchDefaults("p_1", {})).toEqual(launch);
  });
});
