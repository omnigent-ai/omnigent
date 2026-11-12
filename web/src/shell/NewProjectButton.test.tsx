import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { ServerInfo } from "@/lib/capabilities";
import { NewProjectButton } from "./NewProjectButton";

const mocks = vi.hoisted(() => ({
  createProject: vi.fn(),
  createDirectory: vi.fn(),
  projectError: null as Error | null,
  cloneRepository: vi.fn(),
  info: { project_repository_creation: false } as ServerInfo,
  projects: [] as { name: string }[],
}));

vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => mocks.info }));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({
    data: [
      { host_id: "host_1", name: "Laptop", owner: "me", status: "online" },
      { host_id: "host_2", name: "Desktop", owner: "me", status: "online" },
    ],
  }),
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

vi.mock("@/hooks/useHostFilesystem", () => ({
  useCreateHostDirectory: () => ({
    mutateAsync: mocks.createDirectory,
    isPending: false,
  }),
  cloneHostRepository: mocks.cloneRepository,
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

const queryClient = new QueryClient();

function projectButton(hostId: string | null) {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <NewProjectButton onCreated={vi.fn()} hostId={hostId} hostsLoading={false} />
      </TooltipProvider>
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
  mocks.cloneRepository.mockReset();
  mocks.cloneRepository.mockResolvedValue("/Users/me/sample");
  mocks.info = { project_repository_creation: false } as ServerInfo;
  mocks.projects = [];
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

  describe("with repository creation", () => {
    const url = "https://git.example.com/team/sample.git";

    beforeEach(() => {
      mocks.info = {
        project_repository_creation: true,
        managed_sandboxes_enabled: true,
        sandbox_provider: "provider",
        sandbox_providers: ["provider"],
      } as ServerInfo;
    });

    function chooseRepository(project = "Sample") {
      renderButton();
      fireEvent.change(screen.getByPlaceholderText("Project name…"), {
        target: { value: project },
      });
      fireEvent.click(screen.getByRole("button", { name: /Working directory/ }));
      fireEvent.click(screen.getByRole("radio", { name: "Repository" }));
      fireEvent.change(screen.getByLabelText("Repository URL"), { target: { value: url } });
    }

    it("clones a typed URL on the host before saving the project", async () => {
      chooseRepository();
      expect(screen.getByLabelText("Clone into")).toHaveValue("~/sample");
      fireEvent.change(screen.getByLabelText("Repository URL"), { target: { value: "" } });
      fireEvent.click(screen.getByRole("button", { name: "Browse" }));
      fireEvent.click(screen.getByRole("button", { name: "Select test folder" }));
      // A browsed parent keeps following the repository typed afterwards.
      fireEvent.change(screen.getByLabelText("Repository URL"), { target: { value: url } });
      expect(screen.getByLabelText("Clone into")).toHaveValue("/work/repo/sample");
      fireEvent.click(screen.getByTestId("new-project-confirm"));

      await waitFor(() =>
        expect(mocks.createProject).toHaveBeenCalledWith(
          {
            name: "Sample",
            config: {
              host_id: "host_1",
              workspace: "/Users/me/sample",
              repository: { url },
            },
          },
          expect.any(Object),
        ),
      );
      expect(mocks.cloneRepository).toHaveBeenCalledWith("host_1", {
        url,
        branch: undefined,
        path: "/work/repo/sample",
      });
    });

    it("trims the branch once for the clone and the saved project", async () => {
      chooseRepository();
      fireEvent.click(screen.getByTestId("new-project-repo-add"));
      fireEvent.change(screen.getByLabelText("Branch for sample"), {
        target: { value: " release " },
      });
      fireEvent.click(screen.getByTestId("new-project-confirm"));
      await waitFor(() => expect(mocks.createProject).toHaveBeenCalled());
      expect(mocks.cloneRepository).toHaveBeenCalledWith("host_1", {
        url,
        branch: "release",
        path: "~/sample",
      });
      expect(mocks.createProject.mock.calls[0][0].config.repository).toEqual({
        url,
        branch: "release",
      });
    });

    it("keeps the chosen host when no repository URL is entered", async () => {
      renderButton();
      fireEvent.change(screen.getByPlaceholderText("Project name…"), {
        target: { value: "Sample" },
      });
      fireEvent.click(screen.getByRole("button", { name: /Working directory/ }));
      fireEvent.change(screen.getByRole("combobox", { name: "Run on" }), {
        target: { value: "host_2" },
      });
      fireEvent.click(screen.getByRole("radio", { name: "Repository" }));
      fireEvent.click(screen.getByTestId("new-project-confirm"));
      await waitFor(() =>
        expect(mocks.createProject).toHaveBeenCalledWith(
          { name: "Sample", config: { host_id: "host_2" } },
          expect.any(Object),
        ),
      );
      expect(mocks.cloneRepository).not.toHaveBeenCalled();
    });

    it("keeps the modal locked while cloning and keeps inputs after a failure", async () => {
      let rejectClone: (error: Error) => void = () => {};
      mocks.cloneRepository.mockReturnValue(
        new Promise((_resolve, reject) => {
          rejectClone = reject;
        }),
      );
      chooseRepository();
      fireEvent.click(screen.getByTestId("new-project-confirm"));

      await screen.findByText("Cloning…");
      fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      expect(screen.getByLabelText("Repository URL")).toBeDisabled();
      expect(screen.getByTestId("new-project-confirm")).toBeDisabled();

      await act(async () => rejectClone(new Error("The clone destination already exists")));
      expect(screen.getByRole("alert")).toHaveTextContent("already exists");
      expect(screen.getByLabelText("Repository URL")).toHaveValue(url);
      expect(screen.getByLabelText("Repository URL")).toBeEnabled();
      expect(mocks.createProject).not.toHaveBeenCalled();
    });

    it("reuses the checkout when only the project name changes after a failed save", async () => {
      mocks.createProject.mockImplementation(() => {});
      chooseRepository();
      fireEvent.click(screen.getByTestId("new-project-confirm"));
      await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(1));
      fireEvent.change(screen.getByPlaceholderText("Project name…"), {
        target: { value: "Renamed" },
      });
      fireEvent.click(screen.getByTestId("new-project-confirm"));
      await waitFor(() => expect(mocks.createProject).toHaveBeenCalledTimes(2));
      expect(mocks.cloneRepository).toHaveBeenCalledTimes(1);

      fireEvent.change(screen.getByLabelText("Clone into"), { target: { value: "~/other" } });
      fireEvent.click(screen.getByTestId("new-project-confirm"));
      await waitFor(() => expect(mocks.cloneRepository).toHaveBeenCalledTimes(2));
    });

    it("refuses a known duplicate name before cloning", () => {
      mocks.projects = [{ name: "Sample" }];
      chooseRepository();
      fireEvent.click(screen.getByTestId("new-project-confirm"));
      expect(screen.getByRole("alert")).toHaveTextContent("already exists");
      expect(mocks.cloneRepository).not.toHaveBeenCalled();
    });

    it("saves a sandbox repository without cloning", async () => {
      renderButton();
      fireEvent.change(screen.getByPlaceholderText("Project name…"), {
        target: { value: "Sample" },
      });
      fireEvent.click(screen.getByRole("button", { name: /Working directory/ }));
      fireEvent.change(screen.getByRole("combobox", { name: "Run on" }), {
        target: { value: "__sandbox__:provider" },
      });
      expect(screen.queryByLabelText("Clone into")).toBeNull();
      fireEvent.change(screen.getByLabelText("Repository URL"), { target: { value: url } });
      fireEvent.click(screen.getByTestId("new-project-confirm"));

      await waitFor(() =>
        expect(mocks.createProject).toHaveBeenCalledWith(
          {
            name: "Sample",
            config: {
              host_id: "__sandbox__",
              sandbox_provider: "provider",
              repository: { url },
            },
          },
          expect.any(Object),
        ),
      );
      expect(mocks.cloneRepository).not.toHaveBeenCalled();
    });

    it("rejects an invalid URL without side effects", () => {
      renderButton();
      fireEvent.change(screen.getByPlaceholderText("Project name…"), {
        target: { value: "Sample" },
      });
      fireEvent.click(screen.getByRole("button", { name: /Working directory/ }));
      fireEvent.click(screen.getByRole("radio", { name: "Repository" }));
      fireEvent.change(screen.getByLabelText("Repository URL"), {
        target: { value: "not a url" },
      });
      fireEvent.click(screen.getByTestId("new-project-confirm"));
      expect(screen.getByRole("alert")).toHaveTextContent("HTTPS or SSH URL");
      expect(mocks.cloneRepository).not.toHaveBeenCalled();
      expect(mocks.createProject).not.toHaveBeenCalled();
    });
  });

  it("keeps the folder-only form for servers without repository creation", () => {
    renderButton();
    fireEvent.click(screen.getByRole("button", { name: /Working directory/ }));
    expect(screen.queryByRole("combobox", { name: "Run on" })).toBeNull();
    expect(screen.queryByRole("radio", { name: "Repository" })).toBeNull();
  });
});
