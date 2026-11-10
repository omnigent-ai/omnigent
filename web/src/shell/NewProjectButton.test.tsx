import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type { Host } from "@/hooks/useHosts";
import { ApiError } from "@/lib/sessionsApi";
import { authenticatedFetch } from "@/lib/identity";
import { TooltipProvider } from "@/components/ui/tooltip";
import { NewProjectButton } from "./NewProjectButton";

const mocks = vi.hoisted(() => ({
  createProject: vi.fn(),
  createDirectory: vi.fn(),
  cloneRepository: vi.fn(),
  repositoryCreation: false,
  cloneSupported: true,
  detailCloneSupported: true,
  hostStatus: "online",
  projects: [] as { name: string }[],
  projectError: null as Error | null,
}));

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

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

function projectButton(hostId: string | null, contextHost?: Host, queryClient = new QueryClient()) {
  return (
    <QueryClientProvider client={queryClient}>
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
  const queryClient = new QueryClient();
  const view = render(projectButton("host_1", undefined, queryClient));
  fireEvent.click(screen.getByRole("button", { name: "New project" }));
  return {
    rerenderHost: (hostId: string | null) =>
      view.rerender(projectButton(hostId, undefined, queryClient)),
  };
}

beforeEach(() => {
  mocks.createProject.mockReset();
  mocks.createDirectory.mockReset();
  mocks.projectError = null;
  mocks.repositoryCreation = false;
  mocks.cloneSupported = true;
  mocks.detailCloneSupported = true;
  mocks.hostStatus = "online";
  mocks.projects = [];
  mocks.cloneRepository.mockReset().mockResolvedValue("/work/sample");
  vi.mocked(authenticatedFetch)
    .mockReset()
    .mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            host_id: "host_1",
            status: "online",
            clone_repository_supported: mocks.detailCloneSupported,
          }),
        ),
    );
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

async function openRepositoryProject(waitForHostDetail = true) {
  mocks.repositoryCreation = true;
  renderButton();
  fireEvent.change(screen.getByPlaceholderText("Project name…"), { target: { value: "Garden" } });
  fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
  fireEvent.click(screen.getByRole("radio", { name: "Repository" }));
  fireEvent.change(screen.getByRole("combobox", { name: "Repository URL" }), {
    target: { value: "https://git.example.com/team/sample.git" },
  });
  if (waitForHostDetail) {
    await waitFor(() =>
      expect(screen.queryByText("Checking this host's repository support…")).toBeNull(),
    );
  }
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
    await openRepositoryProject();
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
    expect(screen.getByRole("status")).toHaveTextContent(
      "The repository was cloned to /work/sample",
    );
    expect(screen.getByRole("status")).toHaveTextContent("cancel or change this setup");
    expect(screen.getByRole("status")).toHaveTextContent("select this path in Folder mode");
    const branch = screen.getByRole("textbox", { name: "Branch (optional)" });
    fireEvent.change(branch, { target: { value: "release" } });
    fireEvent.change(branch, { target: { value: "" } });
    const destination = screen.getByRole("textbox", { name: "Clone into" });
    fireEvent.change(destination, { target: { value: "/work/other" } });
    fireEvent.change(destination, { target: { value: "~/sample" } });
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
    await openRepositoryProject();
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

  it("checks duplicates and host support before side effects", async () => {
    mocks.projects = [{ name: "Garden" }];
    await openRepositoryProject();
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(screen.getByRole("alert")).toHaveTextContent("already exists");
    expect(mocks.cloneRepository).not.toHaveBeenCalled();
    expect(mocks.createProject).not.toHaveBeenCalled();
  });

  it.each([
    [
      new ApiError("Destination already exists", 409, "clone_destination_conflict"),
      "already exists",
    ],
    [new ApiError("Clone process stopped", 504, "clone_timeout"), "Clone process stopped"],
    [new ApiError("Git could not clone", 502, "clone_failed"), "Git could not clone"],
    [new TypeError("Failed to fetch"), "result is unknown"],
    [new ApiError("Gateway error", 502, null), "result is unknown"],
    [new ApiError("Service unavailable", 503, null), "result is unknown"],
    [new ApiError("Request failed", 400, "unknown_error"), "result is unknown"],
    [new ApiError("Clone connection lost", 504, "clone_transport_error"), "result is unknown"],
  ])("keeps inputs on clone errors and allows correction (%s)", async (error, message) => {
    mocks.cloneRepository.mockRejectedValueOnce(error);
    await openRepositoryProject();
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(await screen.findByRole("alert")).toHaveTextContent(message as string);
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
  it("prevents old-host clone requests and overlong names before side effects", async () => {
    mocks.detailCloneSupported = false;
    await openRepositoryProject();
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

  it("uses the routed detail capability when the list does not advertise clone support", async () => {
    mocks.cloneSupported = false;
    await openRepositoryProject();
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/hosts/host_1?include=capabilities");
    expect(screen.queryByText(/Update this host/)).toBeNull();
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() => expect(mocks.cloneRepository).toHaveBeenCalledOnce());
    expect(mocks.createProject).toHaveBeenCalledOnce();
  });

  it("keeps the selected host and retries a failed capability lookup without inferring support", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response(null, { status: 503 }));
    await openRepositoryProject();
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Couldn't check this host's repository support",
    );
    expect(screen.queryByText(/Update this host/)).toBeNull();
    expect(screen.getByRole("combobox", { name: "Run on" })).toHaveValue("host_1");
    expect(mocks.cloneRepository).not.toHaveBeenCalled();
    expect(mocks.createProject).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Retry host check" }));
    await waitFor(() =>
      expect(screen.queryByText("Checking this host's repository support…")).toBeNull(),
    );
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledOnce());
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
    expect(mocks.cloneRepository).toHaveBeenCalledOnce();
  });

  it("blocks cloning while capability discovery is pending without treating the host as unsupported", async () => {
    let finishLookup!: (response: Response) => void;
    vi.mocked(authenticatedFetch).mockReturnValueOnce(
      new Promise((resolve) => {
        finishLookup = resolve;
      }),
    );
    await openRepositoryProject(false);
    expect(screen.getByText("Checking this host's repository support…")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(screen.getByRole("alert")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Try again shortly");
    expect(screen.queryByText(/Update this host/)).toBeNull();
    expect(mocks.cloneRepository).not.toHaveBeenCalled();
    expect(mocks.createProject).not.toHaveBeenCalled();
    await act(async () =>
      finishLookup(
        new Response(
          JSON.stringify({
            host_id: "host_1",
            status: "online",
            clone_repository_supported: true,
          }),
        ),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
    await waitFor(() =>
      expect(screen.queryAllByText("Checking this host's repository support…")).toHaveLength(0),
    );
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    await waitFor(() => expect(mocks.createProject).toHaveBeenCalledOnce());
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
    await openRepositoryProject();
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
      fireEvent.change(screen.getByPlaceholderText("Project name…"), {
        target: { value: "Garden" },
      });
      fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
      expect(screen.getByRole("combobox", { name: "Run on" })).toHaveValue(hostId);
      expect(
        screen.getByRole("checkbox", { name: "Create a folder using the project name" }),
      ).toBeDisabled();
      const browse = screen.getByRole("button", { name: "Working directory: Not selected" });
      expect(browse).toBeDisabled();
      fireEvent.click(browse);
      expect(screen.queryByRole("button", { name: "Select test folder" })).toBeNull();
      fireEvent.click(screen.getByTestId("new-project-confirm"));
      expect(screen.getByRole("alert")).toHaveTextContent("Reconnect the selected host");
      fireEvent.click(screen.getByRole("radio", { name: "Repository" }));
      expect(screen.getByRole("button", { name: "Browse" })).toBeDisabled();
      fireEvent.click(screen.getByTestId("new-project-confirm"));
      expect(screen.getByRole("alert")).toHaveTextContent("Reconnect the selected host");
      expect(mocks.createDirectory).not.toHaveBeenCalled();
      expect(mocks.cloneRepository).not.toHaveBeenCalled();
      expect(mocks.createProject).not.toHaveBeenCalled();
    },
  );

  it("derives the checkout name from the repository until a full destination is typed", async () => {
    await openRepositoryProject();
    const repositoryInput = screen.getByLabelText("Repository URL");
    const destinationInput = screen.getByRole("textbox", { name: "Clone into" });
    fireEvent.change(repositoryInput, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Browse" }));
    fireEvent.click(screen.getByRole("button", { name: "Select test folder" }));
    expect(destinationInput).toHaveValue("/work/repo/repository");
    fireEvent.change(repositoryInput, {
      target: { value: "https://git.example.com/team/first.git" },
    });
    expect(destinationInput).toHaveValue("/work/repo/first");
    fireEvent.change(repositoryInput, {
      target: { value: "https://git.example.com/team/second.git" },
    });
    expect(destinationInput).toHaveValue("/work/repo/second");
    fireEvent.change(destinationInput, { target: { value: "/work/chosen" } });
    fireEvent.change(repositoryInput, {
      target: { value: "https://git.example.com/team/third.git" },
    });
    expect(destinationInput).toHaveValue("/work/chosen");
    const target = screen.getByRole("combobox", { name: "Run on" });
    fireEvent.change(target, { target: { value: "__sandbox__:example" } });
    fireEvent.change(target, { target: { value: "host_1" } });
    expect(screen.getByRole("textbox", { name: "Clone into" })).toHaveValue("~/third");
  });

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
