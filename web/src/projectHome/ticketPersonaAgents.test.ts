import { describe, expect, it } from "vitest";
import type { ProjectIdentity } from "@/lib/projectsApi";
import { ticketPersonaAgents } from "./ticketPersonaAgents";

function identity(name: string, sessionId: string | null): ProjectIdentity {
  return {
    id: `id-${name}`,
    project_id: "p1",
    name,
    builtin: false,
    instructions: "",
    agent_id: null,
    model: null,
    effort: null,
    active_session_id: sessionId,
    active_session_state: sessionId ? "idle" : "absent",
  };
}

describe("ticketPersonaAgents", () => {
  const identities = [
    identity("Lead", "lead"),
    identity("MCP Engineer", "engineer"),
    identity("Reviewer", "reviewer"),
    identity("PM", "pm"),
    identity("Idle", null),
  ];

  it("lists the assignee and personas that wrote in the activity, except linked ones", () => {
    expect(
      ticketPersonaAgents({
        identities,
        labels: [{ key: "assignee", value: " mcp engineer " }],
        eventSessionIds: new Set(["reviewer", "lead", "pm"]),
        excludeSessionIds: new Set(["lead", "pm"]),
      }),
    ).toEqual([
      { sessionId: "engineer", name: "MCP Engineer", involvement: "Assigned to this ticket" },
      { sessionId: "reviewer", name: "Reviewer", involvement: "Worked on this ticket" },
    ]);
  });

  it("skips personas with no session or no part in the ticket", () => {
    expect(
      ticketPersonaAgents({
        identities,
        labels: [{ key: "assignee", value: "Idle" }],
        eventSessionIds: new Set(),
        excludeSessionIds: new Set(),
      }),
    ).toEqual([]);
  });
});
