import { cleanup, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  RunnerOfflineError,
  type WorkspaceFile,
  useWorkspaceAllFiles,
  useWorkspaceChangedFiles,
} from "@/hooks/useWorkspaceChangedFiles";
import type * as WorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type { FilesTree } from "@/shell/fileTree/FilesTree";

import { FilesPageTree } from "./FilesPageTree";

const captured = vi.hoisted(() => ({ tree: null as ComponentProps<typeof FilesTree> | null }));

vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceChangedFilesModule>()),
  useWorkspaceAllFiles: vi.fn(),
  useWorkspaceChangedFiles: vi.fn(),
}));
vi.mock("@/shell/fileTree/FilesTree", () => ({
  FilesTree: (props: ComponentProps<typeof FilesTree>) => {
    captured.tree = props;
    return <div data-testid="files-tree" />;
  },
}));

function entry(path: string): WorkspaceFile {
  return { path, name: path.split("/").pop()!, type: "file", bytes: 1, modified_at: null };
}

type FilesResult = ReturnType<typeof useWorkspaceAllFiles>;
const listed = (entries: WorkspaceFile[]) =>
  ({ isPending: false, isError: false, data: { available: true, data: entries } }) as FilesResult;

function renderTree(files: Partial<FilesResult>, showHidden = false) {
  vi.mocked(useWorkspaceAllFiles).mockReturnValue(files as FilesResult);
  const onFileSelect = vi.fn();
  render(
    <FilesPageTree
      conversationId="conv_1"
      activePath="src/a.ts"
      sort="alpha"
      showHidden={showHidden}
      onFileSelect={onFileSelect}
    />,
  );
  return { onFileSelect };
}

beforeEach(() => {
  captured.tree = null;
  vi.mocked(useWorkspaceChangedFiles).mockReturnValue({
    data: { available: true, data: [{ path: "src/a.ts" }] },
  } as unknown as ReturnType<typeof useWorkspaceChangedFiles>);
});
afterEach(cleanup);

describe("FilesPageTree", () => {
  it("says it's loading until the listing arrives", () => {
    renderTree({ isPending: true });
    expect(screen.getByRole("status")).toHaveTextContent("Loading files…");
  });

  it("asks for a message to reconnect an offline machine, and alerts on any other failure", () => {
    renderTree({ isPending: false, isError: true, error: new RunnerOfflineError() } as never);
    expect(screen.getByRole("status")).toHaveTextContent(
      "The session’s machine is offline. Send a message to reconnect it.",
    );
    cleanup();
    renderTree({ isPending: false, isError: true, error: new Error("permission denied") } as never);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Couldn’t list the files: permission denied",
    );
  });

  it("explains a session without files, an empty workspace and one that's all hidden", () => {
    renderTree({ isPending: false, isError: false, data: { available: false } } as never);
    expect(screen.getByRole("status")).toHaveTextContent("This session has no workspace files.");
    cleanup();
    renderTree(listed([]));
    expect(screen.getByRole("status")).toHaveTextContent("No files in the workspace.");
    cleanup();
    renderTree(listed([entry(".env")]));
    expect(screen.getByRole("status")).toHaveTextContent(
      "Every file here is hidden. Show hidden files to see them.",
    );
    cleanup();
    renderTree(listed([entry(".env")]), true);
    expect(screen.getByTestId("files-tree")).toBeInTheDocument();
  });

  it("shows the listing with its changes, open down to the page's file", () => {
    const { onFileSelect } = renderTree(listed([entry("README.md"), entry("src")]));
    expect(captured.tree).toMatchObject({
      conversationId: "conv_1",
      rootEntries: [entry("README.md"), entry("src")],
      changedFiles: [{ path: "src/a.ts" }],
      activeFile: "src/a.ts",
      sort: "alpha",
      showHidden: false,
      label: "Workspace files",
    });
    expect(captured.tree?.onFileSelect).toBe(onFileSelect);
  });

  it("shows no changes while the session can't report them", () => {
    vi.mocked(useWorkspaceChangedFiles).mockReturnValue({
      data: { available: false },
    } as unknown as ReturnType<typeof useWorkspaceChangedFiles>);
    renderTree(listed([entry("README.md")]));
    expect(captured.tree?.changedFiles).toEqual([]);
  });
});
