import { describe, expect, it } from "vitest";
import { roleTitle } from "./sdlcTheme.ts";
import {
  addBuiltInRole,
  addRole,
  addTaskType,
  assign,
  coordinatorName,
  isNativeHarness,
  nativeTeam,
  parseTeam,
  raciParticipants,
  registerTeam,
  removeRole,
  removeTaskType,
  sdlcTeam,
  taskTypeOf,
  teamOf,
  teamProblems,
  teamRoleTitle,
  updateRole,
} from "./team.ts";

describe("teams", () => {
  it("builds the native team: the PM alone, on a native harness, with the PO keeping its letters", () => {
    const team = nativeTeam();
    expect(teamProblems(team)).toEqual([]);
    expect(team.roles.map((role) => [role.id, role.title, role.defaultHarness])).toEqual([["pm", "Full Stack Developer (FSD)", "claude-native"]]);
    expect(team.roles[0].brief).toContain("do each task yourself");
    expect(teamRoleTitle(team, "pm")).toBe("Full Stack Developer (FSD)");
    expect(teamRoleTitle(sdlcTeam(), "pm")).toBe("PM");
    // The screen names the open room's coordinator; another room's team restores the theme title.
    registerTeam(team);
    expect([roleTitle("pm"), coordinatorName()]).toEqual(["Full Stack Developer (FSD)", "FSD"]);
    registerTeam(sdlcTeam());
    expect([roleTitle("pm"), coordinatorName()]).toEqual(["PM", "PM"]);
    expect(taskTypeOf(team, "implementation")?.assignments).toEqual({ po: "I", pm: "RA" });
    expect(taskTypeOf(team, "requirements")?.assignments).toEqual({ po: "RA", pm: "C" });
    expect(parseTeam(team)).toEqual({ team });
    expect(["claude-native", "native-codex"].every(isNativeHarness)).toBe(true);
    expect(isNativeHarness("claude-sdk")).toBe(false);
  });

  it("starts from the SDLC team, keeping a room's earlier RACI changes", () => {
    const team = teamOf({ raci: { implementation: { architect: "A", developer: "R" } } });
    expect(team.roles.map((role) => role.id)).toEqual(["pm", "architect", "developer", "reviewer", "qa", "devops"]);
    expect(taskTypeOf(team, "implementation")?.assignments).toMatchObject({ architect: "A", developer: "R" });
    expect(teamProblems(sdlcTeam())).toEqual([]);
    expect(raciParticipants(team).map((entry) => entry.id)).toEqual(["pm", "po", "architect", "developer", "reviewer", "qa", "devops"]);
  });

  it("adds a custom role informed on every task type, with a unique id and its own title", () => {
    const { team, roleId } = addRole(sdlcTeam(), "UX Designer");
    expect(roleId).toBe("ux_designer");
    expect(team.roles.at(-1)).toMatchObject({ id: "ux_designer", title: "UX Designer", builtIn: false });
    expect(team.taskTypes.every((taskType) => taskType.assignments.ux_designer === "I")).toBe(true);
    expect(addRole(team, "UX designer!").roleId).toBe("ux_designer_2");
    // A custom role can never take a built-in id.
    expect(addRole(sdlcTeam(), "QA").roleId).toBe("qa_2");
    registerTeam(team);
    expect(roleTitle("ux_designer")).toBe("UX Designer");
  });

  it("moves accountability when another role becomes accountable", () => {
    const team = assign(sdlcTeam(), "implementation", "architect", "A");
    expect(taskTypeOf(team, "implementation")?.assignments).toMatchObject({ architect: "A", developer: "R" });
    const scope = assign(sdlcTeam(), "scope", "architect", "RA");
    expect(taskTypeOf(scope, "scope")?.assignments).toMatchObject({ architect: "RA", pm: "R" });
  });

  it("reports what a role or task type change breaks", () => {
    // DevOps was the only responsible role for CI/CD.
    expect(teamProblems(removeRole(sdlcTeam(), "devops"))).toContain("Set up CI/CD needs at least one responsible role.");
    expect(teamProblems(removeRole(sdlcTeam(), "pm"))).toEqual([]);
    expect(teamProblems(assign(sdlcTeam(), "test_plan", "qa", "A"))).toEqual(["Create test plans and cases needs at least one responsible role."]);
    const noTypes = sdlcTeam().taskTypes.reduce((team, taskType) => removeTaskType(team, taskType.id), sdlcTeam());
    expect(teamProblems(noTypes)).toEqual(["The team needs at least one task type."]);
  });

  it("adds task types owned by the PM until the PO assigns them, and adds dropped roles back", () => {
    const { team, taskTypeId } = addTaskType(sdlcTeam(), "Write user docs");
    expect(taskTypeOf(team, taskTypeId)).toMatchObject({ title: "Write user docs", gate: null, assignments: { pm: "RA", po: "I", qa: "I" } });
    const dropped = removeRole(team, "reviewer");
    expect(addBuiltInRole(dropped, "reviewer").roles.at(-1)).toMatchObject({ id: "reviewer", title: "Reviewing Developer" });
  });

  it("keeps built-in titles and refuses teams that cannot run", () => {
    const renamed = updateRole(sdlcTeam(), "architect", { title: "Tech Lead", brief: "Lead." });
    expect(renamed.roles[1]).toMatchObject({ title: "Architect", brief: "Lead." });
    const parsed = parseTeam({ ...sdlcTeam(), roles: [...sdlcTeam().roles, { id: "designer", title: "Designer", brief: "Designs.", defaultHarness: "" }] });
    expect("team" in parsed && parsed.team.roles.at(-1)).toMatchObject({ id: "designer", defaultHarness: "claude-sdk" });
    expect("team" in parsed && parsed.team.taskTypes[0].assignments.designer).toBe("I");
    expect(parseTeam({ roles: [], taskTypes: [] })).toMatchObject({ error: expect.stringContaining("The PM must be on the team") });
    expect(parseTeam({ ...sdlcTeam(), roles: [...sdlcTeam().roles, { id: "po", title: "Owner", brief: "x" }] })).toMatchObject({ error: expect.stringContaining("unusable id") });
  });
});
