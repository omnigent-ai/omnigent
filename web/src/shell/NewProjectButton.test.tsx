import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type { Host } from "@/hooks/useHosts";
import { ApiError } from "@/lib/sessionsApi";
import { TooltipProvider } from "@/components/ui/tooltip";
import { NewProjectButton } from "./NewProjectButton";

const mocks = vi.hoisted(() => ({
  createProject: vi.fn(),
  createDirectory: vi.fn(),
  cloneRepository: vi.fn(),
  repositoryCreation: false,
  cloneSupported: true,
  hostStatus: "online",
  projects: [] as { name: string }[],
  projectError: null as Error | null,
}));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({
    data: [
      {
        host_id: "host_1",
        name: "Laptop",
        status: mocks.hostStatus,
        clone_repository_supported: mocks.cloneSupported,
      },
    ],
  }),
}));

vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => ({ data: mocks.projects }),
  useCreateProject: () => ({
    mutate: mocks.createProject,
    isPending: false,
    isError: mocks.projectError !== null,
    error: mocks.projectError,
  }),
}));

vi.mock("@/hooks/useHostFilesystem", () => ({
  useCloneHostRepository: () => ({ mutateAsync: mocks.cloneRepository, isPending: false }),
  useCreateHostDirectory: () => ({
    mutateAsync: mocks.createDirectory,
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

function projectButton(hostId: string | null, contextHost?: Host) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      <CapabilitiesProvider
        info={{
          ...FALLBACK_SERVER_INFO,
          project_repository_creation: mocks.repositoryCreation,
          managed_sandboxes_enabled: true,
          sandbox_provider: "example",
        }}
      >
        <TooltipProvider>
          <NewProjectButton
            onCreated={vi.fn()}
            hostId={hostId}
            contextHost={contextHost}
            hostsLoading={false}
          />
        </TooltipProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>
  );
}

function renderButton() {
  const view = render(projectButton("host_1"));
  fireEvent.click(screen.getByRole("button", { name: "New project" }));
  return {
    rerenderHost: (hostId: string | null) => view.rerender(projectButton(hostId)),
  };
}

beforeEach(() => {
  mocks.createProject.mockReset();
  mocks.createDirectory.mockReset();
  mocks.projectError = null;
  mocks.repositoryCreation = false;
  mocks.cloneSupported = true;
  mocks.hostStatus = "online";
  mocks.projects = [];
  mocks.cloneRepository.mockReset().mockResolvedValue("/work/sample");
  localStorage.clear();
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
          config: {
            host_id: "host_1",
            workspace: "/work/repo",
          },
        },
        expect.any(Object),
      ),
    );
    expect(mocks.createDirectory).not.toHaveBeenCalled();
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
        config: {
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
      (
        input: { name: string },
        options?: {
          onSuccess?: (project: { name: string }) => void;
          onError?: (error: Error) => void;
        },
      ) => {
        projectAttempts += 1;
        if (projectAttempts === 2) {
          options?.onSuccess?.({ name: input.name });
        } else {
          options?.onError?.(new Error("network"));
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

  it("blocks dismissal while creating a folder and saves after it completes", async () => {
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
    expect(screen.queryByRole("button", { name: "Close" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.getByPlaceholderText("Project name…")).toBeInTheDocument();
    await act(async () => resolveDirectory?.("/Users/me/Garden"));
    expect(mocks.createProject).toHaveBeenCalledTimes(1);
    expect(screen.queryByPlaceholderText("Project name…")).toBeNull();
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
});

function openRepositoryProject() {
  mocks.repositoryCreation = true;
  renderButton();
  fireEvent.change(screen.getByPlaceholderText("Project name…"), { target: { value: "Garden" } });
  fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
  fireEvent.click(screen.getByRole("radio", { name: "Repository" }));
  fireEvent.change(screen.getByRole("combobox", { name: "Repository URL" }), {
    target: { value: "https://git.example.com/team/sample.git" },
  });
}

describe("repository project creation", () => {
  it("blocks edits, dismissal and repeated submission through clone and save, reusing a successful clone after save failure", async () => {
    let finishClone!: (path: string) => void;
    let failSave!: (error: Error) => void;
    mocks.cloneRepository.mockReturnValueOnce(
      new Promise<string>((resolve) => {
        finishClone = resolve;
      }),
    );
    mocks.createProject.mockImplementationOnce((_input, callbacks) => {
      failSave = callbacks.onError;
    });
    openRepositoryProject();
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(screen.getByText("Cloning…")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Project name…")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Close" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(mocks.cloneRepository).toHaveBeenCalledTimes(1);
    expect(mocks.createProject).not.toHaveBeenCalled();
    await act(async () => finishClone("/work/sample"));
    expect(screen.getByText("Creating project…")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Project name…")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Close" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(mocks.createProject).toHaveBeenCalledTimes(1);
    act(() => failSave(new Error("network")));
    expect(screen.getByRole("alert")).toHaveTextContent("connection");
    fireEvent.change(screen.getByPlaceholderText("Project name…"), {
      target: { value: "Renamed" },
    });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mocks.cloneRepository).toHaveBeenCalledTimes(1);
    expect(mocks.createProject).toHaveBeenLastCalledWith(
      {
        name: "Renamed",
        config: {
          host_id: "host_1",
          workspace: "/work/sample",
          repository: { url: "https://git.example.com/team/sample.git" },
        },
      },
      expect.any(Object),
    );
  });

  it.each(["Folder", "Repository"])(
    "saves a connected target with an empty optional %s",
    (mode) => {
      mocks.repositoryCreation = true;
      renderButton();
      fireEvent.change(screen.getByPlaceholderText("Project name…"), {
        target: { value: "Garden" },
      });
      fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
      fireEvent.change(screen.getByRole("combobox", { name: "Run on" }), {
        target: { value: "host_1" },
      });
      fireEvent.click(screen.getByRole("radio", { name: mode }));
      fireEvent.click(screen.getByTestId("new-project-confirm"));
      expect(mocks.createProject).toHaveBeenCalledWith(
        { name: "Garden", config: { host_id: "host_1" } },
        expect.any(Object),
      );
      expect(mocks.createDirectory).not.toHaveBeenCalled();
      expect(mocks.cloneRepository).not.toHaveBeenCalled();
    },
  );

  it("saves a typed sandbox repository without cloning or allocating a workspace", async () => {
    openRepositoryProject();
    fireEvent.change(screen.getByRole("combobox", { name: "Run on" }), {
      target: { value: "__sandbox__:example" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Branch (optional)" }), {
      target: { value: " release " },
    });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() =>
      expect(mocks.createProject).toHaveBeenCalledWith(
        {
          name: "Garden",
          config: {
            host_id: "__sandbox__",
            sandbox_provider: "example",
            repository: { url: "https://git.example.com/team/sample.git", branch: "release" },
          },
        },
        expect.any(Object),
      ),
    );
    expect(mocks.cloneRepository).not.toHaveBeenCalled();
  });

  it("checks duplicates and host support before side effects", () => {
    mocks.projects = [{ name: "Garden" }];
    openRepositoryProject();
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(screen.getByRole("alert")).toHaveTextContent("already exists");
    expect(mocks.cloneRepository).not.toHaveBeenCalled();
    expect(mocks.createProject).not.toHaveBeenCalled();
  });

  it.each([
    new ApiError("Destination already exists", 409, "clone_destination_conflict"),
    new TypeError("Failed to fetch"),
  ])("keeps inputs on clone errors and allows correction", async (error) => {
    mocks.cloneRepository.mockRejectedValueOnce(error);
    openRepositoryProject();
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      error instanceof ApiError ? "already exists" : "result is unknown",
    );
    expect(screen.getByRole("combobox", { name: "Repository URL" })).toHaveValue(
      "https://git.example.com/team/sample.git",
    );
    expect(mocks.createProject).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole("textbox", { name: "Clone into" }), {
      target: { value: "/work/other" },
    });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(1));
    expect(mocks.cloneRepository).toHaveBeenLastCalledWith({
      hostId: "host_1",
      url: "https://git.example.com/team/sample.git",
      path: "/work/other",
    });
  });
  it("prevents old-host clone requests and overlong names before side effects", () => {
    mocks.cloneSupported = false;
    openRepositoryProject();
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(screen.getByRole("alert")).toHaveTextContent("Update this host");
    fireEvent.change(screen.getByPlaceholderText("Project name…"), {
      target: { value: "x".repeat(101) },
    });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(screen.getByRole("alert")).toHaveTextContent("100 characters");
    expect(mocks.cloneRepository).not.toHaveBeenCalled();
    expect(mocks.createProject).not.toHaveBeenCalled();
  });

  it("chooses a clone parent and invalidates clone reuse after changing the branch", async () => {
    mocks.cloneRepository
      .mockResolvedValueOnce("/work/repo/sample")
      .mockRejectedValueOnce(
        new ApiError("Destination already exists", 409, "clone_destination_conflict"),
      )
      .mockResolvedValueOnce("/work/repo/sample-release");
    mocks.createProject.mockImplementationOnce((_input, callbacks) =>
      callbacks.onError(new Error("network")),
    );
    openRepositoryProject();
    fireEvent.click(screen.getByRole("button", { name: "Browse" }));
    fireEvent.click(screen.getByRole("button", { name: "Select test folder" }));
    expect(screen.getByRole("textbox", { name: "Clone into" })).toHaveValue("/work/repo/sample");
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await screen.findByRole("alert");
    fireEvent.change(screen.getByRole("textbox", { name: "Branch (optional)" }), {
      target: { value: "release" },
    });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() => expect(mocks.cloneRepository).toHaveBeenCalledTimes(2));
    expect(mocks.cloneRepository).toHaveBeenLastCalledWith({
      hostId: "host_1",
      url: "https://git.example.com/team/sample.git",
      branch: "release",
      path: "/work/repo/sample",
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("already exists");
    expect(mocks.createProject).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByRole("textbox", { name: "Clone into" }), {
      target: { value: "/work/repo/sample-release" },
    });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mocks.cloneRepository).toHaveBeenCalledTimes(3);
    expect(mocks.cloneRepository).toHaveBeenLastCalledWith({
      hostId: "host_1",
      url: "https://git.example.com/team/sample.git",
      branch: "release",
      path: "/work/repo/sample-release",
    });
    expect(mocks.createProject).toHaveBeenLastCalledWith(
      {
        name: "Garden",
        config: {
          host_id: "host_1",
          workspace: "/work/repo/sample-release",
          repository: { url: "https://git.example.com/team/sample.git", branch: "release" },
        },
      },
      expect.any(Object),
    );
  });

  it.each([undefined, "example"])(
    "prefers the active usable context to remembered choices (%s)",
    (provider) => {
      mocks.repositoryCreation = true;
      localStorage.setItem("omnigent:last-host-choice", "missing-host");
      render(
        projectButton("host_1", {
          host_id: "host_1",
          name: "Laptop",
          owner: "me",
          status: "online",
          sandbox_provider: provider,
        }),
      );
      fireEvent.click(screen.getByRole("button", { name: "New project" }));
      fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
      expect(screen.getByRole("combobox", { name: "Run on" })).toHaveValue(
        provider ? "__sandbox__:example" : "host_1",
      );
    },
  );

  it.each(["missing-host", "host_1"])(
    "retains unavailable target %s and disables filesystem controls",
    (hostId) => {
      mocks.repositoryCreation = true;
      mocks.hostStatus = "offline";
      localStorage.setItem("omnigent:last-host-choice", hostId);
      renderButton();
      fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
      expect(screen.getByRole("combobox", { name: "Run on" })).toHaveValue(hostId);
      expect(
        screen.getByRole("checkbox", { name: "Create a folder using the project name" }),
      ).toBeDisabled();
      const browse = screen.getByRole("button", { name: "Working directory: Not selected" });
      expect(browse).toBeDisabled();
      fireEvent.click(browse);
      expect(screen.queryByRole("button", { name: "Select test folder" })).toBeNull();
      fireEvent.click(screen.getByRole("radio", { name: "Repository" }));
      expect(screen.getByRole("button", { name: "Browse" })).toBeDisabled();
      expect(mocks.createDirectory).not.toHaveBeenCalled();
      expect(mocks.cloneRepository).not.toHaveBeenCalled();
    },
  );

  it("blocks a requested folder creation if its host goes offline", () => {
    mocks.repositoryCreation = true;
    const view = renderButton();
    fireEvent.change(screen.getByPlaceholderText("Project name…"), { target: { value: "Garden" } });
    fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Create a folder using the project name" }),
    );
    mocks.hostStatus = "offline";
    view.rerenderHost("host_1");
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(screen.getByRole("alert")).toHaveTextContent("Reconnect the selected host");
    expect(mocks.createDirectory).not.toHaveBeenCalled();
    expect(mocks.createProject).not.toHaveBeenCalled();
  });

  it("uses an available sandbox provider when the remembered provider was removed", () => {
    mocks.repositoryCreation = true;
    localStorage.setItem("omnigent:last-host-choice", "__sandbox__");
    localStorage.setItem("omnigent:last-sandbox-provider", "removed");
    renderButton();
    fireEvent.change(screen.getByPlaceholderText("Project name…"), { target: { value: "Garden" } });
    fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
    expect(screen.getByRole("combobox", { name: "Run on" })).toHaveValue("__sandbox__:example");
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(mocks.createProject).toHaveBeenCalledWith(
      { name: "Garden", config: { host_id: "__sandbox__", sandbox_provider: "example" } },
      expect.any(Object),
    );
    expect(mocks.createDirectory).not.toHaveBeenCalled();
    expect(mocks.cloneRepository).not.toHaveBeenCalled();
  });
});
