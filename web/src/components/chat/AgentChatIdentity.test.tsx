import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentChatIdentity } from "./AgentChatIdentity";

const AGENT = { name: "claude-native-ui", harness: "claude-native", description: null };

describe("AgentChatIdentity", () => {
  it("shows a team member's avatar, with the harness as a badge", () => {
    render(<AgentChatIdentity agent={AGENT} persona="Merger" compact />);
    const orb = screen.getByRole("img", { name: "Merger (Claude Code)" });
    expect(orb.querySelector('[data-persona="Merger"]')).not.toBeNull();
  });

  it("shows the harness for an ordinary session", () => {
    render(<AgentChatIdentity agent={AGENT} compact />);
    const orb = screen.getByRole("img", { name: "Claude Code" });
    expect(orb.querySelector("[data-persona]")).toBeNull();
  });
});
