import { describe, expect, it } from "vitest";
import { modelEfforts, normalizeModelOptions, parseAgyModels, parseCopilotConfigModels } from "../shared/harnessModels.ts";
import { isThisMachine } from "./localModels.ts";
import { OmnigentError } from "./omnigentClient.ts";
import { ModelCatalog } from "./modelCatalog.ts";

const unlisted = (harness: string) => new OmnigentError(502, `model options are unsupported for harness '${harness}'`);

describe("model listings", () => {
  it("keeps launchable ids with their efforts, labels aliases with their model, and drops hidden or duplicate rows", () => {
    const rows = normalizeModelOptions({
      models: [
        { id: "opus", model: "claude-opus-5-5", displayName: "Opus", isDefault: true },
        {
          id: "gpt-6.1-sol",
          model: "gpt-6.1-sol",
          displayName: "GPT-6.1-Sol",
          defaultReasoningEffort: "low",
          supportedReasoningEfforts: [{ reasoningEffort: "low" }, { reasoningEffort: "high" }, { reasoningEffort: "ultra" }],
        },
        { id: "internal", displayName: "Internal", hidden: true },
        { id: "opus", displayName: "Duplicate" },
        { displayName: "No id" },
        "junk",
      ],
    });
    expect(rows).toEqual([
      { id: "opus", label: "Opus (claude-opus-5-5)", isDefault: true, efforts: null, defaultEffort: null },
      { id: "gpt-6.1-sol", label: "GPT-6.1-Sol", isDefault: false, efforts: ["low", "high", "ultra"], defaultEffort: "low" },
    ]);
    expect(normalizeModelOptions(null)).toEqual([]);
    // A model's own efforts are offered only where the harness accepts them.
    expect(modelEfforts(["low", "medium", "high", "xhigh"], rows[1])).toEqual(["low", "high"]);
    expect(modelEfforts(["low", "medium"], rows[0])).toEqual(["low", "medium"]);
  });

  it("parses the agy and Copilot CLI listings", () => {
    expect(parseAgyModels("Fetching available models...\ngemini-4-argon-high\tGemini 4 Argon (High)\nclaude-opus-5-5-low\tClaude Opus 5.5 (Low)\n")).toEqual([
      { id: "gemini-4-argon-high", label: "Gemini 4 Argon (High)", isDefault: false, efforts: null, defaultEffort: null },
      { id: "claude-opus-5-5-low", label: "Claude Opus 5.5 (Low)", isDefault: false, efforts: null, defaultEffort: null },
    ]);
    const help = [
      '  `logLevel`: log level for CLI; defaults to "default".',
      "",
      "  `model`: AI model to use for Copilot CLI; can be changed with /model command.",
      '    - "claude-sonnet-5.5"',
      '    - "gpt-6.1-sol"',
      "",
      "  `contextTier`: context window tier.",
      '    - "default"',
    ].join("\n");
    expect(parseCopilotConfigModels(help).map((model) => model.id)).toEqual(["claude-sonnet-5.5", "gpt-6.1-sol"]);
    expect(parseCopilotConfigModels("no models here")).toEqual([]);
  });

  it("recognizes this machine's host name with or without .local", () => {
    expect(isThisMachine("TofuMacbook", "tofumacbook.local")).toBe(true);
    expect(isThisMachine("build-box", "tofumacbook")).toBe(false);
    expect(isThisMachine(undefined, "tofumacbook")).toBe(false);
  });
});

describe("ModelCatalog", () => {
  it("lists the probed catalog with the harness's efforts, reading the Codex SDK from the Codex CLI probe, and caches it", async () => {
    const calls: string[] = [];
    const catalog = new ModelCatalog({
      hostOptions: async (hostId, harness) => {
        calls.push(`${hostId}:${harness}`);
        return { models: [{ id: "gpt-6.1-sol", displayName: "GPT-6.1-Sol" }] };
      },
      effortFamily: async (harness) => (harness === "codex" ? "openai" : null),
    });
    const first = await catalog.models("host1", "codex");
    expect(first).toMatchObject({ listed: true, source: "host", efforts: ["none", "minimal", "low", "medium", "high", "xhigh"], error: null });
    expect(first.models.map((model) => model.id)).toEqual(["gpt-6.1-sol"]);
    await catalog.models("host1", "codex");
    expect(calls).toEqual(["host1:codex-native"]);
  });

  it("falls back to the harness CLI only for a host on this machine", async () => {
    const local = [{ id: "gemini-4-argon-high", label: "Gemini 4 Argon (High)", isDefault: false, efforts: null, defaultEffort: null }];
    const sources = {
      hostOptions: async (_hostId: string, harness: string) => {
        throw unlisted(harness);
      },
      localModels: async () => local,
      isLocalHost: async (hostId: string) => hostId === "here",
    };
    expect(await new ModelCatalog(sources).models("here", "antigravity-native")).toMatchObject({
      listed: true,
      source: "local_cli",
      models: local,
      efforts: [],
      effortNote: expect.stringContaining("part of the model choice"),
    });
    expect(await new ModelCatalog(sources).models("remote", "antigravity-native")).toMatchObject({ listed: false, models: [], error: null });
  });

  it("reports a failed probe and retries it sooner than a listed one", async () => {
    let now = 0;
    let calls = 0;
    const catalog = new ModelCatalog({
      hostOptions: async () => {
        calls++;
        throw new OmnigentError(409, "host is offline");
      },
      localModels: async () => {
        throw new Error("a failed probe must not fall back to a local CLI");
      },
      isLocalHost: async () => true,
      now: () => now,
    });
    expect(await catalog.models("host1", "claude-sdk")).toMatchObject({ listed: false, error: "host is offline" });
    now = 10_000;
    await catalog.models("host1", "claude-sdk");
    expect(calls).toBe(1);
    now = 20_000;
    await catalog.models("host1", "claude-sdk");
    expect(calls).toBe(2);
  });
});
