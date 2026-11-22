import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type * as CapabilitiesModule from "@/lib/CapabilitiesContext";
import type * as HostsModule from "@/hooks/useHosts";
import type * as AvailableAgentsModule from "@/hooks/useAvailableAgents";
import type * as SandboxModelsModule from "@/hooks/useSandboxModelOptions";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import { sandboxHostChoice, sandboxHostChoiceProvider } from "@/lib/hostPreferences";
import { sandboxOptionLabel } from "@/lib/capabilities";
import { ApiError } from "@/lib/sessionsApi";
import { testAgent } from "@/test/agentFixtures";
import { readLastAgentId, writeLastAgentId } from "@/lib/agentPreferences";
import { writeLastHarness } from "@/lib/harnessPreferences";
import { NewProjectButton } from "./NewProjectButton";

type Host = HostsModule.Host;

const mocks = vi.hoisted(() => ({
  createProject: vi.fn(),
  createDirectory: vi.fn(),
  cloneRepository: vi.fn(),
  projectError: null as Error | null,
  projects: [] as { id: string | null; name: string }[],
  serverInfo: "loading" as unknown,
  agents: [] as AvailableAgentsModule.AvailableAgent[],
}));

vi.mock("@/lib/CapabilitiesContext", async (importOriginal) => ({
  ...(await importOriginal<typeof CapabilitiesModule>()),
  useServerInfo: () => mocks.serverInfo,
}));

vi.mock("@/hooks/useConversations", () => ({
  useCreateProject: () => ({
    mutate: mocks.createProject,
    isPending: false,
    isError: mocks.projectError !== null,
    error: mocks.projectError,
  }),
  useProjects: () => ({ data: mocks.projects }),
}));

vi.mock("@/hooks/useAvailableAgents", async (importOriginal) => ({
  ...(await importOriginal<typeof AvailableAgentsModule>()),
  useAvailableAgents: () => ({ data: mocks.agents, isLoading: false }),
}));

vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof HostsModule>()),
  useHostModelOptions: () => ({
    data: [{ id: "model-a", displayName: "Model A" }],
    isLoading: false,
    error: null,
  }),
}));

vi.mock("@/hooks/useSandboxModelOptions", async (importOriginal) => ({
  ...(await importOriginal<typeof SandboxModelsModule>()),
  useSandboxModelOptions: () => ({
    data: { models: [], configured: false },
    isLoading: false,
    error: null,
  }),
}));

vi.mock("@/lib/agentLabels", async (importOriginal) => {
  const actual = await importOriginal<typeof AgentLabelsModule>();
  return {
    ...actual,
    useBrainHarnessLabels: () => actual.BRAIN_HARNESS_LABELS,
    useHarnessEffortFamilies: () => ({ codex: "openai", "claude-sdk": "anthropic" }),
  };
});

vi.mock("@/hooks/useHostFilesystem", () => ({
  useCreateHostDirectory: () => ({
    mutateAsync: mocks.createDirectory,
    isPending: false,
  }),
  useCloneHostRepository: () => ({
    mutateAsync: mocks.cloneRepository,
    isPending: false,
  }),
}));

vi.mock("@/components/ProjectIconPicker", () => ({
  EmojiPicker: () => null,
}));

vi.mock("./WorkspacePickerDialog", () => ({
  WorkspacePickerDialog: ({
    open,
    onConfirm,
  }: {
    open: boolean;
    onConfirm: (path: string) => void;
  }) =>
    open ? (
      <button type="button" onClick={() => onConfirm("/work/repo")}>
        Select test folder
      </button>
    ) : null,
}));

const laptop = { host_id: "host_1", name: "Laptop", owner: "me", status: "online" } as Host;
const desktop = { host_id: "host_2", name: "Desktop", owner: "me", status: "online" } as Host;
const projectServerInfo = {
  features: { project_repository_creation: true },
  managed_sandboxes_enabled: true,
  sandbox_provider: "modal",
  sandbox_providers: ["modal", "daytona"],
};

function projectButton(hostId: string | null, hosts: Host[] = []) {
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <TooltipProvider>
        <NewProjectButton onCreated={vi.fn()} hostId={hostId} hosts={hosts} hostsLoading={false} />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

function renderButton(hosts: Host[] = [laptop]) {
  const view = render(projectButton("host_1", hosts));
  fireEvent.click(screen.getByRole("button", { name: "New project" }));
  return {
    rerenderHost: (hostId: string | null) => view.rerender(projectButton(hostId, hosts)),
  };
}

beforeEach(() => {
  localStorage.clear();
  mocks.createProject.mockReset();
  mocks.createDirectory.mockReset();
  mocks.cloneRepository.mockReset();
  mocks.cloneRepository.mockResolvedValue("/Users/me/app");
  mocks.projects = [];
  mocks.serverInfo = "loading";
  mocks.projectError = null;
  mocks.agents = [
    testAgent("maestro-id", "maestro", {
      display_name: "Maestro",
      harness: "claude-sdk",
      builtin: true,
    }),
    testAgent("codex-id", "codex-native-ui", {
      display_name: "Codex",
      harness: "codex-native",
      builtin: true,
    }),
  ];
  mocks.createProject.mockImplementation(
    (input: { name: string }, options?: { onSuccess?: (project: { name: string }) => void }) => {
      options?.onSuccess?.({ name: input.name });
    },
  );
  mocks.createDirectory.mockResolvedValue("/Users/me/Garden");
});

describe("NewProjectButton", () => {
  it("stores a selected working directory in the new project config", async () => {
    const { rerenderHost } = renderButton();

    expect(screen.queryByTestId("new-project-workspace")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Working directory/ }));
    fireEvent.click(screen.getByRole("button", { name: "Working directory: Not selected" }));
    fireEvent.click(screen.getByRole("button", { name: "Open folder…" }));
    fireEvent.click(screen.getByRole("button", { name: "Select test folder" }));
    rerenderHost("host_2");
    rerenderHost(null);
    fireEvent.change(screen.getByPlaceholderText("Project name…"), {
      target: { value: "Garden" },
    });
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() =>
      expect(mocks.createProject).toHaveBeenCalledWith(
        {
          name: "Garden",
          idempotencyKey: expect.any(String),
          config: {
            agent_id: "maestro-id",
            harness: "claude-sdk",
            host_id: "host_1",
            workspace: "/work/repo",
          },
        },
        expect.any(Object),
      ),
    );
    expect(mocks.createDirectory).not.toHaveBeenCalled();
  });

  it("does not create the project on the Enter that confirms an IME candidate", async () => {
    renderButton();

    const input = screen.getByPlaceholderText("Project name…");
    fireEvent.change(input, { target: { value: "庭" } });
    // Chrome flags the confirming Enter with keyCode 229; Safari sends a plain
    // Enter before compositionend, so only the tracked composition catches it.
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.compositionEnd(input);
    expect(mocks.createProject).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(mocks.createProject).toHaveBeenCalledWith(
        expect.objectContaining({ name: "庭" }),
        expect.any(Object),
      ),
    );
  });

  it("creates a home folder named after the project and disables browsing", async () => {
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: /Working directory/ }));
    fireEvent.change(screen.getByPlaceholderText("Project name…"), {
      target: { value: "Garden" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "Create a folder using the project name",
      }),
    );

    expect(screen.getByTestId("new-project-workspace")).toBeDisabled();
    expect(screen.getByTestId("new-project-workspace")).toHaveAccessibleName(
      "Working directory: ~/Garden",
    );

    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() =>
      expect(mocks.createDirectory).toHaveBeenCalledWith({
        hostId: "host_1",
        path: "~/Garden",
      }),
    );
    expect(mocks.createProject).toHaveBeenCalledWith(
      {
        name: "Garden",
        idempotencyKey: expect.any(String),
        config: {
          agent_id: "maestro-id",
          harness: "claude-sdk",
          host_id: "host_1",
          workspace: "/Users/me/Garden",
        },
      },
      expect.any(Object),
    );
  });

  it.each(["../garden", "Client/Acme", "Client\\Acme", ".."])(
    "does not create a folder from the path-like project name %s",
    (projectName) => {
      renderButton();

      fireEvent.click(screen.getByRole("button", { name: /Working directory/ }));
      fireEvent.change(screen.getByPlaceholderText("Project name…"), {
        target: { value: projectName },
      });
      fireEvent.click(
        screen.getByRole("checkbox", {
          name: "Create a folder using the project name",
        }),
      );
      fireEvent.click(screen.getByTestId("new-project-confirm"));

      expect(screen.getByRole("alert")).toHaveTextContent(
        "Use a project name without slashes or dot segments to create a folder.",
      );
      expect(mocks.createDirectory).not.toHaveBeenCalled();
      expect(mocks.createProject).not.toHaveBeenCalled();
    },
  );

  it("reuses a created folder when project creation is retried", async () => {
    let projectAttempts = 0;
    mocks.createProject.mockImplementation(
      (input: { name: string }, options?: { onSuccess?: (project: { name: string }) => void }) => {
        projectAttempts += 1;
        if (projectAttempts === 2) {
          options?.onSuccess?.({ name: input.name });
        }
      },
    );
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: /Working directory/ }));
    fireEvent.change(screen.getByPlaceholderText("Project name…"), {
      target: { value: "Garden" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "Create a folder using the project name",
      }),
    );
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(2));
    expect(mocks.createDirectory).toHaveBeenCalledTimes(1);
  });

  it("does not finish a dismissed folder submission or clear a reopened modal", async () => {
    let resolveDirectory: ((path: string) => void) | undefined;
    mocks.createDirectory.mockReturnValueOnce(
      new Promise<string>((resolve) => {
        resolveDirectory = resolve;
      }),
    );
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: /Working directory/ }));
    fireEvent.change(screen.getByPlaceholderText("Project name…"), {
      target: { value: "Garden" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "Create a folder using the project name",
      }),
    );
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));

    await waitFor(() => expect(screen.queryByPlaceholderText("Project name…")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    expect(screen.getByPlaceholderText("Project name…")).toBeInTheDocument();

    await act(async () => resolveDirectory?.("/Users/me/Garden"));

    expect(mocks.createProject).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText("Project name…")).toBeInTheDocument();
  });

  it("turns a duplicate folder error into a short actionable message", async () => {
    mocks.createDirectory.mockRejectedValueOnce(new Error("directory already exists"));
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: /Working directory/ }));
    fireEvent.change(screen.getByPlaceholderText("Project name…"), {
      target: { value: "Garden" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "Create a folder using the project name",
      }),
    );
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That folder already exists. Choose a different folder.",
    );
    expect(mocks.createProject).not.toHaveBeenCalled();
  });

  it("turns a duplicate project error into a short actionable message", () => {
    mocks.projectError = new Error("A project named 'Garden' already exists");
    renderButton();

    expect(screen.getByRole("alert")).toHaveTextContent(
      "A project with this name already exists. Use a different name.",
    );
  });

  it("keeps Run on above Chat and submits the chosen SDK, model and effort", async () => {
    writeLastAgentId("codex-id");
    writeLastHarness("maestro-id", "codex");
    renderButton();
    expect(
      screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent),
    ).toEqual(["Run on", "Chat"]);
    expect(screen.getByTestId("new-chat-landing-agent-select")).toHaveAccessibleName(
      /Maestro, SDK Codex/,
    );
    expect(
      screen
        .getByTestId("new-chat-landing-agent-select")
        .querySelector('[data-agent-icon="maestro"]'),
    ).not.toBeNull();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-agent-config-maestro-id"));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Model A" }));
    fireEvent.click(
      within(screen.getByTestId("project-chat-efforts")).getByRole("menuitemcheckbox", {
        name: "High",
      }),
    );
    fireEvent.keyDown(screen.getByTestId("project-chat-models"), { key: "Escape" });
    fireEvent.change(screen.getByPlaceholderText("Project name…"), { target: { value: "Garden" } });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() =>
      expect(mocks.createProject).toHaveBeenCalledWith(
        {
          name: "Garden",
          config: {
            agent_id: "maestro-id",
            harness: "codex",
            model: "model-a",
            effort: "high",
            host_id: "host_1",
          },
          idempotencyKey: expect.any(String),
        },
        expect.any(Object),
      ),
    );
    expect(readLastAgentId()).toBe("codex-id");
  });

  it("allows choosing another agent instead of Maestro", async () => {
    renderButton();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-agent-codex-id"));
    fireEvent.change(screen.getByPlaceholderText("Project name…"), { target: { value: "Garden" } });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() =>
      expect(mocks.createProject).toHaveBeenCalledWith(
        expect.objectContaining({
          config: { agent_id: "codex-id", host_id: "host_1" },
        }),
        expect.any(Object),
      ),
    );
    expect(readLastAgentId()).toBeNull();
  });

  it("reuses the operation key after an uncertain response even when the project appears in the list", async () => {
    mocks.createProject.mockImplementation((_input, options) =>
      options.onError(new Error("network request failed")),
    );
    renderButton();
    fireEvent.change(screen.getByPlaceholderText("Project name…"), { target: { value: "Garden" } });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await screen.findByRole("alert");
    mocks.projects = [{ id: "created-project", name: "Garden" }];
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(2));
    expect(mocks.createProject.mock.calls[1][0]).toEqual(mocks.createProject.mock.calls[0][0]);
    fireEvent.change(screen.getByPlaceholderText("Project name…"), {
      target: { value: "Another project" },
    });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(3));
    expect(mocks.createProject.mock.calls[2][0].idempotencyKey).not.toBe(
      mocks.createProject.mock.calls[0][0].idempotencyKey,
    );
  });

  it("keeps launch validation errors visible without clearing the choices", async () => {
    mocks.createProject.mockImplementation((_input, options) =>
      options.onError(new ApiError("Codex is not available on this host.", 400, null)),
    );
    renderButton();
    fireEvent.change(screen.getByPlaceholderText("Project name…"), { target: { value: "Garden" } });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Codex is not available on this host.",
    );
    expect(screen.getByPlaceholderText("Project name…")).toHaveValue("Garden");
  });
});

describe("NewProjectButton project repositories", () => {
  function selectRunOn(value: string) {
    const label =
      [laptop, desktop].find((host) => host.host_id === value)?.name ??
      sandboxOptionLabel(sandboxHostChoiceProvider(value) ?? null);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Run on" }), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: new RegExp(`^${label}`) }));
  }

  function typeRepository(url: string, branch?: string) {
    fireEvent.change(screen.getByLabelText("Repository URL"), { target: { value: url } });
    if (branch !== undefined) {
      fireEvent.change(screen.getByLabelText("Branch"), { target: { value: branch } });
    }
  }

  function nameProject(value: string) {
    fireEvent.change(screen.getByPlaceholderText("Project name…"), { target: { value } });
  }

  function chooseRepositorySource() {
    fireEvent.click(screen.getByRole("radio", { name: "Repository" }));
  }

  const repoUrl = "https://git.example.com/team/app.git";

  it("clones the repository before saving the project with its checkout", async () => {
    mocks.serverInfo = projectServerInfo;
    renderButton([laptop]);

    nameProject("Garden");
    chooseRepositorySource();
    typeRepository(repoUrl, "main");
    expect(screen.getByLabelText("Clone into")).toHaveValue("~/app");
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(1));
    expect(mocks.cloneRepository).toHaveBeenCalledWith({
      hostId: "host_1",
      url: repoUrl,
      branch: "main",
      path: "~/app",
    });
    expect(mocks.cloneRepository.mock.invocationCallOrder[0]).toBeLessThan(
      mocks.createProject.mock.invocationCallOrder[0],
    );
    expect(mocks.createProject).toHaveBeenCalledWith(
      {
        name: "Garden",
        idempotencyKey: expect.any(String),
        config: {
          agent_id: "maestro-id",
          harness: "claude-sdk",
          host_id: "host_1",
          workspace: "/Users/me/app",
          repository: { url: repoUrl, branch: "main" },
        },
      },
      expect.any(Object),
    );
  });

  it("keeps the clone destination following the repository name until the parent is browsed", async () => {
    mocks.serverInfo = projectServerInfo;
    renderButton([laptop]);

    nameProject("Garden");
    chooseRepositorySource();
    typeRepository(repoUrl);
    fireEvent.click(screen.getByRole("button", { name: "Browse" }));
    fireEvent.click(screen.getByRole("button", { name: "Select test folder" }));
    expect(screen.getByLabelText("Clone into")).toHaveValue("/work/repo/app");

    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() =>
      expect(mocks.cloneRepository).toHaveBeenCalledWith(
        expect.objectContaining({ path: "/work/repo/app" }),
      ),
    );
  });

  it("locks the dialog while the clone runs and keeps the inputs when it fails", async () => {
    mocks.serverInfo = projectServerInfo;
    let rejectClone: ((error: Error) => void) | undefined;
    mocks.cloneRepository.mockReturnValueOnce(
      new Promise<string>((_resolve, reject) => {
        rejectClone = reject;
      }),
    );
    renderButton([laptop]);

    nameProject("Garden");
    chooseRepositorySource();
    typeRepository(repoUrl, "main");
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(screen.getByLabelText("Clone into")).toBeDisabled());
    expect(screen.getByTestId("new-project-confirm")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByPlaceholderText("Project name…")).toBeInTheDocument();

    await act(async () => rejectClone?.(new Error("Host did not respond")));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The repository couldn’t be cloned. Check the details and try again.",
    );
    expect(screen.getByLabelText("Repository URL")).toHaveValue(repoUrl);
    expect(screen.getByLabelText("Clone into")).toHaveValue("~/app");
    expect(screen.getByLabelText("Clone into")).not.toBeDisabled();
    expect(mocks.createProject).not.toHaveBeenCalled();
  });

  it("does not tell the user to delete a checkout the host may still be writing", async () => {
    mocks.serverInfo = projectServerInfo;
    mocks.cloneRepository.mockRejectedValueOnce(
      new Error(
        "host 'laptop' did not finish cloning within 420s; the clone may still finish, so check the destination before retrying",
      ),
    );
    renderButton([laptop]);

    nameProject("Garden");
    chooseRepositorySource();
    typeRepository(repoUrl);
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "It may still finish, so check the destination before retrying.",
    );
    expect(alert).not.toHaveTextContent("Delete the partial folder");
  });

  it("does not save a folder created for the previous Run on target", async () => {
    mocks.serverInfo = projectServerInfo;
    let resolveDirectory: ((path: string) => void) | undefined;
    mocks.createDirectory.mockReturnValueOnce(
      new Promise<string>((resolve) => {
        resolveDirectory = resolve;
      }),
    );
    renderButton([laptop, desktop]);

    nameProject("Garden");
    fireEvent.click(screen.getByRole("button", { name: /Working directory/ }));
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "Create a folder using the project name",
      }),
    );
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() => expect(mocks.createDirectory).toHaveBeenCalledTimes(1));

    selectRunOn("host_2");
    await act(async () => resolveDirectory?.("/Users/me/Garden"));

    expect(mocks.createProject).not.toHaveBeenCalled();
  });

  it("reuses a successful checkout when only the name changes after a failed save", async () => {
    mocks.serverInfo = projectServerInfo;
    mocks.createProject.mockImplementationOnce(
      (_input: unknown, options?: { onError?: (error: Error) => void }) => {
        options?.onError?.(new Error("network request failed"));
      },
    );
    renderButton([laptop]);

    nameProject("Garden");
    chooseRepositorySource();
    typeRepository(repoUrl);
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The repository is already checked out at /Users/me/app.",
    );

    nameProject("Garden two");
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(2));
    expect(mocks.cloneRepository).toHaveBeenCalledTimes(1);
    expect(mocks.createProject.mock.calls[1][0].config).toMatchObject({
      workspace: "/Users/me/app",
    });
  });

  it("refuses a name that another project already uses", () => {
    mocks.serverInfo = projectServerInfo;
    mocks.projects = [{ id: "project_1", name: "Garden" }];
    renderButton([laptop]);

    nameProject("Garden");
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "A project with this name already exists. Use a different name.",
    );
    expect(mocks.cloneRepository).not.toHaveBeenCalled();
    expect(mocks.createProject).not.toHaveBeenCalled();
  });

  it.each([true, false])(
    "creates a project from a label-only legacy project (feature enabled: %s)",
    async (enabled) => {
      mocks.serverInfo = {
        ...projectServerInfo,
        features: { project_repository_creation: enabled },
      };
      mocks.projects = [{ id: null, name: "Garden" }];
      renderButton([laptop]);

      nameProject("Garden");
      fireEvent.click(screen.getByTestId("new-project-confirm"));

      await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(1));
      expect(mocks.createProject.mock.calls[0][0].name).toBe("Garden");
    },
  );

  it("keeps a branch typed before Add when the URL is added to the selection", async () => {
    mocks.serverInfo = projectServerInfo;
    renderButton([laptop]);

    nameProject("Garden");
    chooseRepositorySource();
    typeRepository(repoUrl, "release");
    fireEvent.click(screen.getByTestId("new-project-repo-add"));
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(1));
    expect(mocks.cloneRepository).toHaveBeenCalledWith(
      expect.objectContaining({ url: repoUrl, branch: "release" }),
    );
    expect(mocks.createProject.mock.calls[0][0].config.repository).toEqual({
      url: repoUrl,
      branch: "release",
    });
  });

  it("rejects a branch typed without a repository URL", () => {
    mocks.serverInfo = projectServerInfo;
    renderButton([laptop]);

    nameProject("Garden");
    chooseRepositorySource();
    typeRepository("", "main");
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Add the repository this branch belongs to.",
    );
    expect(mocks.createProject).not.toHaveBeenCalled();
  });

  it("rejects an invalid repository URL before any side effect", () => {
    mocks.serverInfo = projectServerInfo;
    renderButton([laptop]);

    nameProject("Garden");
    chooseRepositorySource();
    typeRepository("not a url");
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Enter a repository URL like https://github.com/org/repo.",
    );
    expect(mocks.cloneRepository).not.toHaveBeenCalled();
    expect(mocks.createProject).not.toHaveBeenCalled();
  });

  it("trims the repository URL and branch once for the clone and the saved config", async () => {
    mocks.serverInfo = projectServerInfo;
    renderButton([laptop]);

    nameProject("Garden");
    chooseRepositorySource();
    typeRepository(`  ${repoUrl}  `, "  main ");
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(1));
    expect(mocks.cloneRepository).toHaveBeenCalledWith(
      expect.objectContaining({ url: repoUrl, branch: "main" }),
    );
    expect(mocks.createProject.mock.calls[0][0].config.repository).toEqual({
      url: repoUrl,
      branch: "main",
    });
  });

  it("saves a sandbox repository and provider without cloning", async () => {
    mocks.serverInfo = projectServerInfo;
    renderButton([laptop]);

    nameProject("Garden");
    selectRunOn(sandboxHostChoice("modal"));
    expect(screen.queryByRole("radio", { name: "Folder" })).toBeNull();
    typeRepository(repoUrl, "dev");
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(1));
    expect(mocks.cloneRepository).not.toHaveBeenCalled();
    expect(mocks.createProject).toHaveBeenCalledWith(
      {
        name: "Garden",
        idempotencyKey: expect.any(String),
        config: {
          agent_id: "maestro-id",
          harness: "claude-sdk",
          host_id: "__sandbox__",
          sandbox_provider: "modal",
          repository: { url: repoUrl, branch: "dev" },
        },
      },
      expect.any(Object),
    );
  });

  it("saves a picked host without a workspace in folder mode", async () => {
    mocks.serverInfo = projectServerInfo;
    renderButton([laptop, desktop]);

    selectRunOn("host_2");
    nameProject("Garden");
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(1));
    expect(mocks.createProject).toHaveBeenCalledWith(
      {
        name: "Garden",
        config: { agent_id: "maestro-id", harness: "claude-sdk", host_id: "host_2" },
        idempotencyKey: expect.any(String),
      },
      expect.any(Object),
    );
  });

  it("saves the visible host and launch choices even without a working directory", async () => {
    mocks.serverInfo = projectServerInfo;
    renderButton([laptop]);

    nameProject("Garden");
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(1));
    expect(mocks.createProject).toHaveBeenCalledWith(
      {
        name: "Garden",
        config: { agent_id: "maestro-id", harness: "claude-sdk", host_id: "host_1" },
        idempotencyKey: expect.any(String),
      },
      expect.any(Object),
    );
  });

  it("saves the picked host in repository mode even without a URL", async () => {
    mocks.serverInfo = projectServerInfo;
    renderButton([laptop, desktop]);

    selectRunOn("host_2");
    chooseRepositorySource();
    nameProject("Garden");
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(1));
    expect(mocks.cloneRepository).not.toHaveBeenCalled();
    expect(mocks.createProject).toHaveBeenCalledWith(
      {
        name: "Garden",
        config: { agent_id: "maestro-id", harness: "claude-sdk", host_id: "host_2" },
        idempotencyKey: expect.any(String),
      },
      expect.any(Object),
    );
  });

  it("offers no repository source when there is no host to run on", () => {
    mocks.serverInfo = projectServerInfo;
    render(projectButton(null, []));
    fireEvent.click(screen.getByRole("button", { name: "New project" }));

    expect(screen.getByRole("button", { name: "Run on" })).toHaveTextContent("Choose host");
    expect(screen.queryByRole("radio", { name: "Repository" })).toBeNull();
    expect(screen.queryByLabelText("Repository URL")).toBeNull();
  });

  it.each([
    ["an older server omits the feature", {}],
    ["the feature is turned off", { project_repository_creation: false }],
  ])("keeps the folder-only dialog when %s", (_label, features) => {
    mocks.serverInfo = { managed_sandboxes_enabled: true, sandbox_providers: ["modal"], features };
    renderButton([laptop]);

    expect(screen.getByRole("button", { name: "Run on" })).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "Repository" })).toBeNull();
    expect(screen.getByRole("button", { name: /Working directory/ })).toBeInTheDocument();
  });
});
