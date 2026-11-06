import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { NewProjectButton } from "./NewProjectButton";

const mocks = vi.hoisted(() => ({
  createProject: vi.fn(),
  createDirectory: vi.fn(),
  projectError: null as Error | null,
}));

vi.mock("@/hooks/useConversations", () => ({
  useCreateProject: () => ({
    mutate: mocks.createProject,
    isPending: false,
    isError: mocks.projectError !== null,
    error: mocks.projectError,
  }),
}));

vi.mock("@/hooks/useHostFilesystem", () => ({
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

function projectButton(hostId: string | null) {
  return (
    <TooltipProvider>
      <NewProjectButton onCreated={vi.fn()} hostId={hostId} hostsLoading={false} />
    </TooltipProvider>
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
