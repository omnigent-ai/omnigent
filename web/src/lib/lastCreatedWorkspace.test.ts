import { beforeEach, describe, expect, it } from "vitest";

import {
  LAST_CREATED_WORKSPACE_STORAGE_KEY,
  readLastCreatedWorkspace,
  writeLastCreatedWorkspace,
} from "./lastCreatedWorkspace";

beforeEach(() => localStorage.clear());

describe("lastCreatedWorkspace", () => {
  it("stores the successful-create directory independently for each host", () => {
    writeLastCreatedWorkspace("host_a", "/srv/alpha");
    writeLastCreatedWorkspace("host_b", "D:\\Projects\\Beta");

    expect(readLastCreatedWorkspace("host_a")).toBe("/srv/alpha");
    expect(readLastCreatedWorkspace("host_b")).toBe("D:\\Projects\\Beta");
    expect(readLastCreatedWorkspace("host_c")).toBeNull();
  });

  it("preserves legal trailing spaces instead of applying recent-list trimming", () => {
    const path = "/srv/projects/folder-with-trailing-space ";
    writeLastCreatedWorkspace("host_a", path);

    expect(readLastCreatedWorkspace("host_a")).toBe(path);
    expect(JSON.parse(localStorage.getItem(LAST_CREATED_WORKSPACE_STORAGE_KEY) ?? "{}")).toEqual({
      host_a: path,
    });
  });

  it("ignores missing hosts and empty paths", () => {
    writeLastCreatedWorkspace(null, "/srv/alpha");
    writeLastCreatedWorkspace("host_a", "");

    expect(localStorage.getItem(LAST_CREATED_WORKSPACE_STORAGE_KEY)).toBeNull();
  });
});
