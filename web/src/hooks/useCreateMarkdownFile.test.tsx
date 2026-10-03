import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceAllFilesResult, WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

const mocks = vi.hoisted(() => ({
  allFiles: { data: undefined as WorkspaceAllFilesResult | undefined },
  mutate: vi.fn(),
}));

vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceAllFiles: () => mocks.allFiles,
}));
vi.mock("@/hooks/useWriteFileContent", () => ({
  useWriteFileContent: () => ({ mutate: mocks.mutate }),
}));

import { newMarkdownFileName, useCreateMarkdownFile } from "./useCreateMarkdownFile";

function file(path: string, type: WorkspaceFile["type"] = "file"): WorkspaceFile {
  const name = path.split("/").pop() ?? path;
  return { path, name, type, bytes: 0, modified_at: null };
}

afterEach(() => {
  mocks.mutate.mockReset();
  mocks.allFiles.data = undefined;
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
    const openFile = vi.fn();
    const { result } = renderHook(() => useCreateMarkdownFile("conv_1", openFile));

    result.current();

    expect(mocks.mutate).toHaveBeenCalledTimes(1);
    const [vars, opts] = mocks.mutate.mock.calls[0];
    expect(vars).toEqual({ path: "untitled-2.md", content: "" });
    // The viewer opens only once the write lands.
    expect(openFile).not.toHaveBeenCalled();
    opts.onSuccess();
    expect(openFile).toHaveBeenCalledWith("untitled-2.md");
  });

  it("falls back to untitled.md before the file list has loaded", () => {
    const openFile = vi.fn();
    const { result } = renderHook(() => useCreateMarkdownFile("conv_1", openFile));

    result.current();

    expect(mocks.mutate.mock.calls[0][0]).toEqual({ path: "untitled.md", content: "" });
  });
});
