import { describe, it, expect } from "vitest";
import { BRAIN_HARNESS_LABELS, capitalizeAgentName } from "./agentLabels";

describe("BRAIN_HARNESS_LABELS", () => {
  it("contains all expected harness entries", () => {
    expect(BRAIN_HARNESS_LABELS["claude-sdk"]).toBe("Claude SDK");
    expect(BRAIN_HARNESS_LABELS["openai-agents"]).toBe("OpenAI Agents SDK");
    expect(BRAIN_HARNESS_LABELS["codex"]).toBe("Codex");
    expect(BRAIN_HARNESS_LABELS["pi"]).toBe("Pi");
    expect(BRAIN_HARNESS_LABELS["rovo-cli"]).toBe("Rovo Dev");
  });

  it("does not include native harnesses", () => {
    expect(BRAIN_HARNESS_LABELS["claude-native"]).toBeUndefined();
    expect(BRAIN_HARNESS_LABELS["codex-native"]).toBeUndefined();
  });
});

describe("capitalizeAgentName", () => {
  it("capitalizes the first letter", () => {
    expect(capitalizeAgentName("polly")).toBe("Polly");
  });

  it("returns empty string unchanged", () => {
    expect(capitalizeAgentName("")).toBe("");
  });
});
