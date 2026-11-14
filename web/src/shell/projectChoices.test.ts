import { describe, expect, it } from "vitest";

import { canCreateProject, projectsMatching } from "./projectChoices";

const PROJECTS = [{ name: "Payments" }, { name: "Sprint 42" }, { name: "Pay later" }];

describe("projectsMatching", () => {
  it("matches part of a name in any case, ignoring outer spaces", () => {
    expect(projectsMatching(PROJECTS, "  PAY ")).toEqual([
      { name: "Payments" },
      { name: "Pay later" },
    ]);
    expect(projectsMatching(PROJECTS, "t 4")).toEqual([{ name: "Sprint 42" }]);
  });

  it("lists every project, as given, for an empty or blank query", () => {
    expect(projectsMatching(PROJECTS, "")).toBe(PROJECTS);
    expect(projectsMatching(PROJECTS, "   ")).toBe(PROJECTS);
  });

  it("lists nothing when no name contains the query", () => {
    expect(projectsMatching(PROJECTS, "roadmap")).toEqual([]);
  });
});

describe("canCreateProject", () => {
  it("offers a name no project has", () => {
    expect(canCreateProject(PROJECTS, "Roadmap")).toBe(true);
    expect(canCreateProject(PROJECTS, "Pay")).toBe(true);
    expect(canCreateProject([], "Roadmap")).toBe(true);
  });

  it("doesn't offer a name an existing project has in another case or with outer spaces", () => {
    expect(canCreateProject(PROJECTS, "payments")).toBe(false);
    expect(canCreateProject(PROJECTS, " SPRINT 42 ")).toBe(false);
  });

  it("doesn't offer an empty or blank name", () => {
    expect(canCreateProject(PROJECTS, "")).toBe(false);
    expect(canCreateProject(PROJECTS, "   ")).toBe(false);
  });
});
