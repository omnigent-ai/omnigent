import { describe, expect, it } from "vitest";

import {
  effortChoicesForModel,
  modelApplyState,
  planSessionSwitch,
  sessionSwitchHarness,
  switchBaselineChanged,
  type SwitchDraft,
  type SwitchSnapshot,
} from "./sessionSwitch";
import type { NativeModelOption } from "./types";

const CLAUDE: NativeModelOption[] = [{ id: "opus" }, { id: "sonnet" }];
const CODEX: NativeModelOption[] = [
  {
    id: "gpt-5.5",
    supportedReasoningEfforts: [{ reasoningEffort: "low" }, { reasoningEffort: "high" }],
  },
];

const bound: SwitchSnapshot = {
  hostId: "host_a",
  runnerId: "runner_a",
  harness: "claude-native",
  modelOverride: "opus",
  llmModel: "opus",
  reasoningEffort: "high",
  status: "idle",
  costControlModeOverride: null,
};

function draft(overrides: Partial<SwitchDraft> = {}): SwitchDraft {
  return {
    accountId: "claude_personal",
    hostId: "host_a",
    harness: "claude-native",
    model: "opus",
    effort: "high",
    workspace: null,
    ...overrides,
  };
}

describe("planSessionSwitch", () => {
  it("returns no steps for the current selection", () => {
    expect(planSessionSwitch(bound, draft(), CLAUDE)).toEqual([]);
  });

  it("re-sends a stored request that an idle session doesn't report", () => {
    const snapshot = { ...bound, modelOverride: "sonnet", llmModel: "opus" };
    expect(modelApplyState(snapshot, CLAUDE, "sonnet")).toBe("unapplied");
    expect(planSessionSwitch(snapshot, draft({ model: "sonnet" }), CLAUDE)).toEqual([
      { kind: "update", updates: { modelOverride: "sonnet" } },
    ]);
  });

  it("waits instead of re-sending while nothing is reported or the runner starts", () => {
    const unreported = { ...bound, modelOverride: "sonnet", llmModel: null };
    expect(modelApplyState(unreported, CLAUDE, "sonnet")).toBe("pending");
    expect(planSessionSwitch(unreported, draft({ model: "sonnet" }), CLAUDE)).toEqual([]);
    const starting = { ...bound, modelOverride: "sonnet", status: "launching" as const };
    expect(modelApplyState(starting, CLAUDE, "sonnet")).toBe("pending");
    expect(planSessionSwitch(starting, draft({ model: "sonnet" }), CLAUDE)).toEqual([]);
  });

  it("re-sends fields whose last write failed even when stored values match", () => {
    const unreported = { ...bound, llmModel: null };
    expect(planSessionSwitch(unreported, draft(), CLAUDE, { model: true, effort: true })).toEqual([
      {
        kind: "update",
        updates: { modelOverride: "opus", reasoningEffort: "high" },
      },
    ]);
    expect(planSessionSwitch(bound, draft(), CLAUDE, { effort: true })).toEqual([
      { kind: "update", updates: { reasoningEffort: "high" } },
    ]);
  });

  it("turns Smart Routing off when pinning the same model", () => {
    const routed = { ...bound, modelOverride: null, costControlModeOverride: "on" as const };
    expect(planSessionSwitch(routed, draft(), CLAUDE)).toEqual([
      {
        kind: "update",
        updates: { modelOverride: "opus", costControlModeOverride: "off" },
      },
    ]);
  });

  it("writes only the changed field for a same-host change", () => {
    expect(planSessionSwitch(bound, draft({ effort: null }), CLAUDE)).toEqual([
      { kind: "update", updates: { reasoningEffort: null } },
    ]);
  });

  it("records the transition, writes the settings, then launches for a move", () => {
    expect(planSessionSwitch(bound, draft({ hostId: "host_b", workspace: "/w" }), CLAUDE)).toEqual([
      { kind: "begin-transition", draft: draft({ hostId: "host_b", workspace: "/w" }) },
      {
        kind: "update",
        updates: {
          modelOverride: "opus",
          reasoningEffort: "high",
          costControlModeOverride: "off",
          silent: true,
        },
      },
      { kind: "launch", hostId: "host_b", workspace: "/w" },
    ]);
  });

  it("resumes a recorded transition without recording it again", () => {
    const marker = {
      id: "t1",
      account_id: "claude_personal",
      host_id: "host_b",
      harness: "claude-native",
      model: "opus",
      effort: "high",
    };
    const stranded = { ...bound, runnerId: null, accountTransition: marker };
    const steps = planSessionSwitch(stranded, draft({ hostId: "host_b", workspace: "/w" }), CLAUDE);
    expect(steps.map((step) => step.kind)).toEqual(["update", "launch"]);
    expect(() =>
      planSessionSwitch(stranded, draft({ hostId: "host_c", workspace: "/w" }), CLAUDE),
    ).toThrow(/pending account change/);
  });

  it("normalizes a provider model id to the catalog id it matches", () => {
    const rows = [{ id: "opus", model: "claude-opus-4-7" }];
    const steps = planSessionSwitch(
      { ...bound, modelOverride: "sonnet" },
      draft({ model: "claude-opus-4-7" }),
      rows,
    );
    expect(steps).toEqual([{ kind: "update", updates: { modelOverride: "opus" } }]);
  });

  it("refuses a model or effort the catalog doesn't offer", () => {
    expect(() => planSessionSwitch(bound, draft({ model: "haiku" }), CLAUDE)).toThrow(/catalog/);
    expect(() =>
      planSessionSwitch(
        { ...bound, harness: "codex-native" },
        draft({ harness: "codex-native", model: "gpt-5.5", effort: "max" }),
        CODEX,
      ),
    ).toThrow(/effort/);
  });

  it("requires a directory for a launch", () => {
    expect(() => planSessionSwitch(bound, draft({ hostId: "host_b" }), CLAUDE)).toThrow(
      /directory/,
    );
  });
});

describe("helpers", () => {
  it("detects a changed baseline", () => {
    expect(switchBaselineChanged(bound, { ...bound, status: "running" })).toBe(false);
    expect(switchBaselineChanged(bound, { ...bound, runnerId: null })).toBe(true);
    expect(switchBaselineChanged(bound, { ...bound, modelOverride: "sonnet" })).toBe(true);
  });

  it("uses catalog efforts, falling back to the Claude ladder", () => {
    expect(effortChoicesForModel("codex-native", CODEX[0]!)).toEqual(["low", "high"]);
    expect(effortChoicesForModel("claude-native", { id: "opus" })).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
    expect(effortChoicesForModel("codex-native", { id: "bare" })).toEqual([]);
  });

  it("resolves the harness from the wrapper label when harness is absent", () => {
    expect(
      sessionSwitchHarness({ harness: null, labels: { "omnigent.wrapper": "codex-native-ui" } }),
    ).toBe("codex-native");
    expect(sessionSwitchHarness({ harness: "pi-native", labels: {} })).toBeNull();
  });
});
