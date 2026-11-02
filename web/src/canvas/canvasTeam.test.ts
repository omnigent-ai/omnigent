import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import {
  isTeamLeadRole,
  teamBranchName,
  teamEdges,
  teamRole,
  TEAM_ROLE_LABEL_KEY,
} from "./canvasTeam";

function session(id: string, role?: string): Conversation {
  return { id, labels: role ? { [TEAM_ROLE_LABEL_KEY]: role } : {} } as unknown as Conversation;
}

describe("canvasTeam", () => {
  it("reads a role and recognises the lead however it is cased", () => {
    expect(teamRole(session("a", " Backend "))).toBe("Backend");
    expect(teamRole(session("a"))).toBeNull();
    expect(isTeamLeadRole("team LEAD")).toBe(true);
    expect(isTeamLeadRole("Backend")).toBe(false);
    expect(isTeamLeadRole(null)).toBe(false);
  });

  it("joins the lead to every other card, including sessions without a role", () => {
    const edges = teamEdges([
      session("work"),
      session("lead", "Team lead"),
      session("api", "Backend"),
    ]);
    expect(edges.map((edge) => [edge.source, edge.target])).toEqual([
      ["lead", "work"],
      ["lead", "api"],
    ]);
  });

  it("draws nothing without a lead", () => {
    expect(teamEdges([session("work"), session("api", "Backend")])).toEqual([]);
  });

  it("makes a valid branch name from any role", () => {
    expect(teamBranchName("Front End!", "4f2a")).toBe("team/front-end-4f2a");
    expect(teamBranchName("***", "4f2a")).toBe("team/agent-4f2a");
  });
});
