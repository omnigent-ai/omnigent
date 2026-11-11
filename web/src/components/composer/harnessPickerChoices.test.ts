import { describe, expect, it } from "vitest";

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";

import { harnessPickerChoices, type HarnessPickerInput } from "./harnessPickerChoices";

function agent(id: string, harness: string, name = `${id}-ui`): AvailableAgent {
  return {
    id,
    name,
    display_name: id,
    description: null,
    harness,
    skills: [],
    builtin: true,
  } as AvailableAgent;
}

const CLAUDE = agent("claude", "claude-native");
const CURSOR = agent("cursor", "cursor-native");
const CODEX = agent("codex", "codex-native");
const OPENCODE = agent("opencode", "opencode-native");
const PI = agent("pi", "pi-native");
const GOOSE = agent("goose", "goose-native");
const KIRO = agent("kiro", "kiro-native");
const CUSTOM = agent("custom", "custom-harness");

// An online host; harnesses it doesn't list have unknown readiness and stay launchable.
function host(configured: Record<string, unknown> = {}): Host {
  return {
    host_id: "host_1",
    name: "laptop",
    owner: "me",
    status: "online",
    configured_harnesses: configured,
  } as unknown as Host;
}

function input(overrides: Partial<HarnessPickerInput> = {}): HarnessPickerInput {
  return {
    harnessEntries: [CLAUDE, CODEX, OPENCODE],
    effectiveAgentId: null,
    host: host(),
    hideUnconfigured: false,
    hiddenAgentNames: new Set(),
    promotedHarnessId: null,
    ...overrides,
  };
}

const ids = (entries: readonly AvailableAgent[] | undefined) => entries?.map((entry) => entry.id);

function rows(overrides: Partial<HarnessPickerInput> = {}) {
  const choices = harnessPickerChoices(input(overrides));
  return {
    ready: ids(choices.readyHarnesses),
    more: ids(choices.moreHarnesses),
    hidOffered: choices.hidOfferedHarness,
    curated: ids(choices.curated),
  };
}

describe("harnessPickerChoices", () => {
  it("lists the preferred harnesses inline in preferred order and the rest under Other", () => {
    expect(rows({ harnessEntries: [GOOSE, PI, CODEX, OPENCODE, CLAUDE, CURSOR] })).toEqual({
      ready: ["claude", "cursor", "codex"],
      more: ["opencode", "pi", "goose"],
      hidOffered: false,
      curated: undefined,
    });
  });

  it("keeps the roster's order between entries of the same rank", () => {
    const secondClaude = agent("claude-2", "claude-native", "claude-2-ui");
    expect(rows({ harnessEntries: [KIRO, secondClaude, GOOSE, CLAUDE] })).toMatchObject({
      ready: ["claude-2", "claude"],
      more: ["kiro", "goose"],
    });
  });

  it("keeps a harness inline while its config flyout is open", () => {
    expect(rows({ promotedHarnessId: "opencode" })).toMatchObject({
      ready: ["claude", "codex", "opencode"],
      more: [],
    });
  });

  it("moves a preferred harness that can't launch under Other, unless it's selected", () => {
    const codexMissing = host({ "codex-native": "binary-missing" });
    expect(rows({ host: codexMissing })).toMatchObject({
      ready: ["claude"],
      more: ["opencode", "codex"],
    });
    expect(rows({ host: codexMissing, effectiveAgentId: "codex" })).toMatchObject({
      ready: ["claude", "codex"],
      more: ["opencode"],
    });
  });

  it("drops harnesses that can't launch when hiding unconfigured ones, but never the selected one", () => {
    const missing = host({ "codex-native": "binary-missing", "opencode-native": "binary-missing" });
    expect(rows({ host: missing, hideUnconfigured: true })).toMatchObject({
      ready: ["claude"],
      more: [],
    });
    expect(
      rows({ host: missing, hideUnconfigured: true, effectiveAgentId: "opencode" }),
    ).toMatchObject({ ready: ["claude"], more: ["opencode"] });
  });

  it("hides entries hidden in Settings, except the selected one, and says it hid an offered row", () => {
    const hiddenAgentNames = new Set(["codex-ui", "claude-ui"]);
    // With both preferred harnesses hidden, the backfill moves the launchable rest inline.
    expect(rows({ hiddenAgentNames })).toMatchObject({
      ready: ["opencode"],
      more: [],
      hidOffered: true,
    });
    expect(rows({ hiddenAgentNames, effectiveAgentId: "claude" })).toMatchObject({
      ready: ["claude"],
      hidOffered: true,
    });
  });

  it("doesn't count a hide when hiding unconfigured harnesses already dropped the row", () => {
    expect(
      rows({
        host: host({ "codex-native": "binary-missing" }),
        hideUnconfigured: true,
        hiddenAgentNames: new Set(["codex-ui"]),
      }),
    ).toMatchObject({ ready: ["claude"], hidOffered: false });
  });

  it("promotes up to three launchable harnesses inline when no preferred one is left", () => {
    expect(
      rows({
        harnessEntries: [GOOSE, KIRO, OPENCODE, PI, CUSTOM],
        host: host({ "opencode-native": "binary-missing" }),
      }),
    ).toMatchObject({ ready: ["pi", "goose", "kiro"], more: ["opencode", "custom"] });
  });

  it("promotes nothing when no harness can launch", () => {
    const missing = host({ "opencode-native": "binary-missing", "pi-native": "binary-missing" });
    expect(rows({ harnessEntries: [OPENCODE, PI], host: missing })).toMatchObject({
      ready: [],
      more: ["opencode", "pi"],
    });
  });

  it("keeps a curated list's order and applies the same hides", () => {
    const missing = host({ "codex-native": "binary-missing", "opencode-native": "binary-missing" });
    const curatedEntries = [OPENCODE, CUSTOM, CODEX, CLAUDE];
    expect(rows({ curatedEntries }).curated).toEqual(["opencode", "custom", "codex", "claude"]);
    expect(rows({ curatedEntries, hiddenAgentNames: new Set(["claude-ui"]) }).curated).toEqual([
      "opencode",
      "custom",
      "codex",
    ]);
    // Only the roster's harnesses are checked for readiness; other entries always stay.
    expect(
      rows({
        curatedEntries,
        host: missing,
        hideUnconfigured: true,
        effectiveAgentId: "codex",
        harnessEntries: [CLAUDE, CODEX, OPENCODE],
      }).curated,
    ).toEqual(["custom", "codex", "claude"]);
  });

  it("leaves its inputs unchanged", () => {
    const harnessEntries = Object.freeze([GOOSE, OPENCODE, CODEX, CLAUDE]);
    const curatedEntries = Object.freeze([CODEX, GOOSE]);
    harnessPickerChoices(input({ harnessEntries, curatedEntries }));
    expect(ids(harnessEntries)).toEqual(["goose", "opencode", "codex", "claude"]);
    expect(ids(curatedEntries)).toEqual(["codex", "goose"]);
  });
});
