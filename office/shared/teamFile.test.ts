import { describe, expect, it } from "vitest";
import { addRole, assign, nativeTeam, sdlcTeam, teamOf } from "./team.ts";
import { parseTeamFile, teamFileName, teamFileYaml, templateFileYaml } from "./teamFile.ts";
import { NATIVE_TEAM_TEMPLATE, templateRolesFromRuntime } from "./teamTemplates.ts";
import type { BranchRuntime } from "./workflowTypes.ts";

const tools = (role: string) => ["office_board_get", "office_link_publish", ...(role === "pm" ? ["office_task_dispatch"] : [])];

describe("team files", () => {
  it("round-trips a room's team, RACI, and role setups through YAML", () => {
    const { team: withDesigner, roleId } = addRole(sdlcTeam(), "UX Designer");
    const team = assign(withDesigner, "implementation", roleId, "C");
    const runtime: BranchRuntime = {
      team,
      roleHarnesses: { qa: "codex" },
      roleModels: { pm: "opus" },
      roleEfforts: { pm: "high" },
      roleCustomInstructions: { qa: "Test in Safari too." },
      roleToolSettings: { developer: { disabledTools: ["office_link_publish"], riskPolicy: "deny" } },
    };
    const yaml = teamFileYaml(runtime, "Design team", "With a designer");
    expect(yaml).toContain("kind: omnigent-office-team");
    expect(yaml).toContain("title: UX Designer");
    const parsed = parseTeamFile(yaml, tools);
    if ("error" in parsed) throw new Error(parsed.error);
    expect(parsed).toMatchObject({ name: "Design team", description: "With a designer" });
    expect(parsed.team).toEqual(teamOf(runtime));
    expect(parsed.roles).toEqual(templateRolesFromRuntime(runtime));
  });

  it("keeps the native team's renamed agent", () => {
    const parsed = parseTeamFile(templateFileYaml(NATIVE_TEAM_TEMPLATE), tools);
    if ("error" in parsed) throw new Error(parsed.error);
    expect(parsed.team).toEqual(nativeTeam());
    expect(parsed.name).toBe("Native (single agent)");
  });

  it("refuses files the office cannot run, saying why", () => {
    const yaml = teamFileYaml({}, "Team");
    expect(parseTeamFile("roles: [", tools)).toMatchObject({ error: expect.stringContaining("not valid YAML") });
    expect(parseTeamFile("kind: something-else\n", tools)).toMatchObject({ error: expect.stringContaining("not a team file") });
    expect(parseTeamFile(yaml.replace("version: 1", "version: 2"), tools)).toMatchObject({ error: expect.stringContaining("version 2") });
    // Taking the PM out leaves a team without its coordinator.
    expect(parseTeamFile(yaml.replace("  - id: pm\n", "  - id: pmx\n"), tools)).toMatchObject({ error: expect.stringContaining("The PM must be on the team") });
    expect(parseTeamFile(`${yaml}\n`.replace("defaultHarness: codex\n", "defaultHarness: codex\n    tools:\n      disabledTools: [office_board_get_all]\n"), tools)).toMatchObject({
      error: expect.stringContaining("office_board_get_all is not an office tool"),
    });
  });

  it("names files after the team", () => {
    expect(teamFileName("Claude-heavy Team!")).toBe("claude-heavy-team.team.yaml");
    expect(teamFileName("***")).toBe("team.team.yaml");
  });
});
