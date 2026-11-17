import { describe, expect, it } from "vitest";
import { projectHomePath, projectNameFromHomePath } from "./projectHomeNavigation";

describe("project home navigation", () => {
  it.each(["", "/ml/omnigent-embed"])("round-trips a project name under mount %j", (mount) => {
    expect(projectHomePath("My Project")).toBe("/projects/My%20Project");
    expect(projectNameFromHomePath(`${mount}${projectHomePath("My Project")}`)).toBe("My Project");
    expect(projectNameFromHomePath(`${mount}${projectHomePath("a/b")}/`)).toBe("a/b");
    expect(projectNameFromHomePath(`${mount}/projects/%E0`)).toBeNull();
    expect(projectNameFromHomePath(`${mount}/projects`)).toBeNull();
    expect(projectNameFromHomePath(`${mount}/projects/a/extra`)).toBeNull();
    expect(projectNameFromHomePath(`${mount}/c/session`)).toBeNull();
  });
});
