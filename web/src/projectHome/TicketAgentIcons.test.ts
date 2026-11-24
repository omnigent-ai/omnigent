import { describe, expect, it } from "vitest";
import { ticketAgentOrbs } from "./TicketAgentIcons";

describe("ticketAgentOrbs", () => {
  it("gives each persona its own orb and groups other sessions by agent", () => {
    const orbs = ticketAgentOrbs([
      { agent_name: "claude-native-ui", harness: "claude-native", persona: "Lead" },
      { agent_name: "claude-native-ui", harness: "claude-native", persona: "Reviewer" },
      { agent_name: "claude-native-ui", harness: "claude-native", persona: "Reviewer" },
      { agent_name: "claude-native-ui", harness: "claude-native", persona: null },
      { agent_name: "claude-native-ui", harness: "claude-native" },
    ]);
    const keys = [...orbs.keys()];
    expect(keys.slice(0, 2)).toEqual(["persona:Lead", "persona:Reviewer"]);
    expect(keys).toHaveLength(3);
    expect(orbs.get("persona:Reviewer")).toEqual({ persona: "Reviewer" });
    expect("agent" in orbs.get(keys[2])!).toBe(true);
  });
});
