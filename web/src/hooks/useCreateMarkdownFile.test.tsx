import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceAllFilesResult, WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

const mocks = vi.hoisted(() => ({
  allFiles: {
    data: undefined as WorkspaceAllFilesResult | undefined,
    isSuccess: false,
    isError: false,
  },
  mutate: vi.fn(),
  isPending: false,
  toastError: vi.fn(),
}));

vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceAllFiles: () => mocks.allFiles,
}));
vi.mock("@/hooks/useWriteFileContent", () => ({
  useCreateFileContent: () => ({ mutate: mocks.mutate, isPending: mocks.isPending }),
}));
vi.mock("sonner", () => ({ toast: { error: mocks.toastError } }));

import { newMarkdownFileName, useCreateMarkdownFile } from "./useCreateMarkdownFile";

function file(path: string, type: WorkspaceFile["type"] = "file"): WorkspaceFile {
  const name = path.split("/").pop() ?? path;
  return { path, name, type, bytes: 0, modified_at: null };
}

afterEach(() => {
  mocks.mutate.mockReset();
  mocks.allFiles.data = undefined;
  mocks.allFiles.isSuccess = false;
  mocks.allFiles.isError = false;
  mocks.isPending = false;
  mocks.toastError.mockReset();
});

describe("newMarkdownFileName", () => {
  it("uses untitled.md when it is free", () => {
    expect(newMarkdownFileName([])).toBe("untitled.md");
    expect(newMarkdownFileName(["notes.md", "README.md"])).toBe("untitled.md");
  });

  it("increments past existing untitled files, reusing the lowest free gap", () => {
    expect(newMarkdownFileName(["untitled.md"])).toBe("untitled-2.md");
    expect(newMarkdownFileName(["untitled.md", "untitled-2.md"])).toBe("untitled-3.md");
    expect(newMarkdownFileName(["untitled.md", "untitled-3.md"])).toBe("untitled-2.md");
  });
});

describe("useCreateMarkdownFile", () => {
  it("writes a unique empty .md at the root and opens it on success", () => {
    mocks.allFiles.data = {
      available: true,
      // A root untitled.md is taken; the directory and the NESTED untitled-2.md
      // must not count against root names.
      data: [file("untitled.md"), file("src", "directory"), file("src/untitled-2.md")],
    };
    mocks.allFiles.isSuccess = true;
    const openFile = vi.fn();
    const { result } = renderHook(() => useCreateMarkdownFile("conv_1", openFile));

    result.current.create();

    expect(mocks.mutate).toHaveBeenCalledTimes(1);
    const [vars, opts] = mocks.mutate.mock.calls[0];
    expect(vars).toEqual({ path: "untitled-2.md", content: "" });
    // The viewer opens only once the write lands.
    expect(openFile).not.toHaveBeenCalled();
    opts.onSuccess();
    expect(openFile).toHaveBeenCalledWith("untitled-2.md");
  });

  it("does not create while the file list is loading", () => {
    const openFile = vi.fn();
    const { result } = renderHook(() => useCreateMarkdownFile("conv_1", openFile));

    expect(result.current.disabled).toBe(true);
    result.current.create();

    expect(mocks.mutate).not.toHaveBeenCalled();
  });

  it("does not create after the file list errors", () => {
    mocks.allFiles.isError = true;
    const openFile = vi.fn();
    const { result } = renderHook(() => useCreateMarkdownFile("conv_1", openFile));

    result.current.create();

    expect(mocks.mutate).not.toHaveBeenCalled();
  });

  it("treats a root directory as occupied and prevents concurrent creates", () => {
    mocks.allFiles.data = {
      available: true,
      data: [file("untitled.md", "directory")],
    };
    mocks.allFiles.isSuccess = true;
    const openFile = vi.fn();
    const { result } = renderHook(() => useCreateMarkdownFile("conv_1", openFile));

    result.current.create();
    result.current.create();

    expect(mocks.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.mutate.mock.calls[0][0]).toEqual({ path: "untitled-2.md", content: "" });
  });

  it("stays disabled while a create is pending and reports atomic conflicts", () => {
    mocks.allFiles.data = { available: true, data: [] };
    mocks.allFiles.isSuccess = true;
    mocks.isPending = true;
    const openFile = vi.fn();
    const { result, rerender } = renderHook(() => useCreateMarkdownFile("conv_1", openFile));

    expect(result.current.disabled).toBe(true);
    result.current.create();
    expect(mocks.mutate).not.toHaveBeenCalled();

    mocks.isPending = false;
    rerender();
    result.current.create();
    const options = mocks.mutate.mock.calls[0][1];
    options.onError(new Error("409 Conflict: Path already exists"));

    expect(openFile).not.toHaveBeenCalled();
    expect(mocks.toastError).toHaveBeenCalledWith(
      "Couldn't create untitled.md: 409 Conflict: Path already exists",
    );
  });
});
