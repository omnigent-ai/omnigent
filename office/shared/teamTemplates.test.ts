import { describe, expect, it } from "vitest";
import { addRole, addTaskType, removeRole, sdlcTeam } from "./team.ts";
import { DEFAULT_TEAM_TEMPLATE, type TeamTemplate, runtimeWithTemplate, templateDiff, templateRolesFromRuntime } from "./teamTemplates.ts";
import type { BranchRuntime } from "./workflowTypes.ts";

const room: BranchRuntime = {
  hostId: "host",
  workspace: "/repo",
  roleHarnesses: { developer: "codex", qa: "claude-sdk" },
  roleModels: { developer: "gpt-6.1-sol" },
  roleEfforts: { developer: "high" },
  roleCustomInstructions: { qa: "Test in Safari too." },
  roleToolSettings: { devops: { riskPolicy: "deny", updatedAt: 5 } },
};

describe("team templates", () => {
  it("captures only what differs from the office defaults", () => {
    expect(templateRolesFromRuntime(room)).toEqual({
      developer: { harness: "codex", model: "gpt-6.1-sol", effort: "high" },
      qa: { instructions: "Test in Safari too." },
      devops: { toolSettings: { riskPolicy: "deny" } },
    });
    // A room-wide harness override counts for every role it moves off its default.
    expect(templateRolesFromRuntime({ harness: "codex" }).developer).toEqual({ harness: "codex" });
  });

  it("lists what switching would change, and nothing for the room's own template", () => {
    const own: TeamTemplate = { ...DEFAULT_TEAM_TEMPLATE, id: "t1", builtIn: false, roles: templateRolesFromRuntime(room) };
    expect(templateDiff(room, own)).toEqual({ roles: [], taskTypes: [] });
    expect(templateDiff(room, DEFAULT_TEAM_TEMPLATE).roles).toEqual([
      { role: "developer", title: "Developer", fields: ["harness", "model", "effort"] },
      { role: "qa", title: "QA", fields: ["instructions"] },
      { role: "devops", title: "DevOps", fields: ["tools"] },
    ]);
  });

  it("switches the team but keeps where the room runs", () => {
    const template: TeamTemplate = { ...DEFAULT_TEAM_TEMPLATE, id: "t2", builtIn: false, roles: { pm: { harness: "claude-sdk", model: "opus" } } };
    const next = runtimeWithTemplate({ ...room, harness: "copilot", model: "x" }, template, 99);
    expect(next).toMatchObject({ hostId: "host", workspace: "/repo", teamTemplateId: "t2", roleHarnesses: { pm: "claude-sdk" }, roleModels: { pm: "opus" } });
    expect(next.harness).toBeUndefined();
    expect(next.roleCustomInstructions).toEqual({});
    // DevOps lost its policy, so its running session is marked out of date.
    expect(next.roleToolSettings).toEqual({ devops: { updatedAt: 99 } });
    expect(templateDiff(next, template)).toEqual({ roles: [], taskTypes: [] });
  });

  it("carries a custom team, listing roles joining and leaving and task type changes", () => {
    const withDesigner = addRole(removeRole(sdlcTeam(), "reviewer"), "Designer");
    const custom = addTaskType(withDesigner.team, "Design screens").team;
    const template: TeamTemplate = { ...DEFAULT_TEAM_TEMPLATE, id: "t3", builtIn: false, team: custom, roles: { designer: { model: "opus" } } };
    const diff = templateDiff({}, template);
    expect(diff.roles).toEqual(expect.arrayContaining([
      { role: "designer", title: "Designer", fields: ["added"] },
      { role: "reviewer", title: "Reviewing Developer", fields: ["removed"] },
    ]));
    expect(diff.taskTypes).toEqual(['adds "Design screens"']);
    const next = runtimeWithTemplate({ roleModels: { reviewer: "x" } }, template, 1);
    expect(next.team).toEqual(custom);
    expect(next.roleModels).toEqual({ designer: "opus" });
  });
});
