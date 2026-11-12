import { describe, expect, it } from "vitest";

import { groupByModelCreator, matchesModelSearch, modelCreator } from "@/lib/modelCreators";

describe("modelCreator", () => {
  it.each([
    // One creator through several hosts.
    ["anthropic/claude-sonnet-4-5", undefined, "Anthropic"],
    ["amazon-bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0", undefined, "Anthropic"],
    ["google-vertex/claude-sonnet-4-5@20250929", undefined, "Anthropic"],
    ["openrouter/anthropic/claude-sonnet-4.5", undefined, "Anthropic"],
    ["omnigent/databricks-claude-sonnet-4-6", undefined, "Anthropic"],
    ["opus[1m]", "system.ai.claude-opus-4-8[1m]", "Anthropic"],
    ["openrouter/google/gemini-2.5-pro", undefined, "Google"],
    ["google-vertex/gemini-2.5-pro", undefined, "Google"],
    // GLM and Kimi stay apart from OpenAI, including on OpenAI-compatible surfaces.
    ["omnigent-openai/system.ai.gpt-5-6-sol", undefined, "OpenAI"],
    ["omnigent-openai/system.ai.glm-5-2", undefined, "Z.ai"],
    ["databricks-glm-5-2", undefined, "Z.ai"],
    ["huggingface/zai-org/GLM-4.6", undefined, "Z.ai"],
    ["mistral/zai-glm-5-2", undefined, "Z.ai"],
    ["databricks-kimi-k2-7-code", undefined, "Moonshot AI"],
    ["amazon-bedrock/moonshot.kimi-k2-thinking", undefined, "Moonshot AI"],
    ["nvidia/moonshotai/kimi-k2.6", undefined, "Moonshot AI"],
    // An organization's fine-tune belongs to it, not to its base model's lab.
    ["openrouter/nvidia/llama-3.1-nemotron-70b-instruct", undefined, "NVIDIA"],
    ["groq/llama-3.3-70b-versatile", undefined, "Meta"],
    ["fireworks/accounts/fireworks/models/qwen3p8-max", undefined, "Alibaba / Qwen"],
  ])("attributes %s to its creator", (id, model, label) => {
    expect(modelCreator({ id, model }).label).toBe(label);
  });

  it.each([
    [{ id: "radius/balanced", displayName: "Balanced" }],
    [{ id: "auto", displayName: "Auto" }],
    [{ id: "openrouter/openrouter/auto" }],
    [{ id: "openrouter/sao10k/l3.3-euryale-70b", displayName: "Llama 3.3 Euryale 70B" }],
  ])("keeps unidentified rows under Other models: %o", (option) => {
    expect(modelCreator(option)).toEqual({ id: "other", label: "Other models" });
  });

  it("uses a display name only for an alias without an organization", () => {
    expect(modelCreator({ id: "kimi-coding/k3", displayName: "Kimi K3" }).label).toBe(
      "Moonshot AI",
    );
    expect(modelCreator({ id: "fast", displayName: "Claude Haiku 4.5" }).label).toBe("Anthropic");
  });
});

describe("groupByModelCreator", () => {
  it("orders known creators first and Other models last, keeping catalog order", () => {
    const ids = [
      "radius/balanced",
      "openai/gpt-5.5",
      "anthropic/claude-opus-5",
      "openai/gpt-5.5-mini",
      "anthropic/claude-haiku-5-5",
    ];
    const groups = groupByModelCreator(ids, (id) => ({ id }));
    expect(groups.map(({ creator, items }) => [creator.label, items])).toEqual([
      ["Anthropic", ["anthropic/claude-opus-5", "anthropic/claude-haiku-5-5"]],
      ["OpenAI", ["openai/gpt-5.5", "openai/gpt-5.5-mini"]],
      ["Other models", ["radius/balanced"]],
    ]);
  });
});

describe("matchesModelSearch", () => {
  const kimi = { id: "omnigent/databricks-kimi-k2-7-code", displayName: "Kimi K2.7 Code" };

  it.each(["moonshot", "kimi code", "databricks-kimi", "K2.7"])("matches %s", (query) => {
    expect(matchesModelSearch(kimi, "Kimi K2.7 Code", query)).toBe(true);
  });

  it("requires every term", () => {
    expect(matchesModelSearch(kimi, "Kimi K2.7 Code", "kimi gpt")).toBe(false);
  });
});
