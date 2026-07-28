import { describe, expect, it } from "vitest";
import { parseWorkspaceFileKey, workspaceFileKey } from "./workspaceFiles";

describe("workspace file identities", () => {
  it("keeps default-root keys backwards compatible", () => {
    expect(workspaceFileKey("default", "src/App.tsx")).toBe("src/App.tsx");
    expect(parseWorkspaceFileKey("src/App.tsx")).toEqual({
      environmentId: "default",
      path: "src/App.tsx",
    });
  });

  it("keeps identical relative paths in different roots distinct", () => {
    const first = workspaceFileKey("dir_00000000000000000000000000000001", "README.md");
    const second = workspaceFileKey("dir_00000000000000000000000000000002", "README.md");
    expect(first).not.toBe(second);
    expect(parseWorkspaceFileKey(first)).toEqual({
      environmentId: "dir_00000000000000000000000000000001",
      path: "README.md",
    });
  });
});
