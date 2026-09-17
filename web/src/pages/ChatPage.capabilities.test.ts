import { describe, expect, it } from "vitest";
import {
  effortLevelsForConv,
  modelPickerKindForConv,
  shouldShowEffortPicker,
  shouldShowModelPicker,
} from "./ChatPage";

// These pin the composer capability gates (effort levels, model picker, effort
// picker). Wrapper labels are authoritative; the resolved harness is the fallback
// for any session with no presentation label — chat-first custom Codex agents,
// and sessions created before their harness was renamed (the ACP-era Devin rows).
// Label-less sessions on a NON-native harness still fail closed, and so does a
// label-less sub-agent child.

const NATIVE = "claude-code-native-ui";

describe("effortLevelsForConv", () => {
  it("returns the extended ladder (xhigh, max) for claude-code-native-ui", () => {
    // WHY: claude-native exposes the full reasoning ladder; dropping xhigh/max
    // here would silently cap those sessions at "high".
    expect(effortLevelsForConv({ labels: { "omnigent.wrapper": NATIVE } })).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
  });

  it("returns only the selected model's rungs for devin-native-ui", () => {
    // WHY: Devin has no --effort flag — effort is a suffix on the model id, and
    // the rungs are per model. swe-2 has no low/xhigh (`swe-2-low` is a different
    // Fusion model), so offering them would compose an id Devin resolves back to
    // the bare family, and the pick would look like it did nothing.
    const conv = { labels: { "omnigent.wrapper": "devin-native-ui" } };
    const catalog = [
      {
        id: "swe-2",
        supportedReasoningEfforts: [
          { reasoningEffort: "medium" },
          { reasoningEffort: "high" },
          { reasoningEffort: "max" },
        ],
      },
    ];
    expect(effortLevelsForConv(conv, catalog, "swe-2")).toEqual(["medium", "high", "max"]);
    // No catalog yet, or a model with no effort dimension: nothing to offer.
    expect(effortLevelsForConv(conv)).toEqual([]);
  });

  it("returns the base three levels for a non-native wrapper", () => {
    // WHY: other wrappers only support low/medium/high; offering xhigh/max
    // would send an effort the harness can't honor.
    expect(effortLevelsForConv({ labels: { "omnigent.wrapper": "codex-native" } })).toEqual([
      "low",
      "medium",
      "high",
    ]);
  });

  it("falls back to the base ladder when labels / conv are absent", () => {
    // WHY: a null conv (pre-hydration) or label-less row must fail to the
    // safe base ladder, not crash.
    expect(effortLevelsForConv(null)).toEqual(["low", "medium", "high"]);
    expect(effortLevelsForConv(undefined)).toEqual(["low", "medium", "high"]);
    expect(effortLevelsForConv({ labels: {} })).toEqual(["low", "medium", "high"]);
  });
});

describe("shouldShowModelPicker", () => {
  it("shows the picker for the native wrappers that honor a model override", () => {
    // WHY: the model picker writes a model override the runner injects as
    // --model at launch; claude, codex, and cursor native wrappers all honor
    // it, so the gate is keyed on those exact labels.
    expect(shouldShowModelPicker({ labels: { "omnigent.wrapper": NATIVE } })).toBe(true);
    expect(shouldShowModelPicker({ labels: { "omnigent.wrapper": "codex-native-ui" } })).toBe(true);
    expect(shouldShowModelPicker({ labels: { "omnigent.wrapper": "cursor-native-ui" } })).toBe(
      true,
    );
    // opencode mirrors its live TUI model into model_override (like cursor), so
    // the model indicator surfaces it and reflects in-TUI switches.
    expect(shouldShowModelPicker({ labels: { "omnigent.wrapper": "opencode-native-ui" } })).toBe(
      true,
    );
    // kiro applies the picked model as --model at launch (no in-session mirror).
    expect(shouldShowModelPicker({ labels: { "omnigent.wrapper": "kiro-native-ui" } })).toBe(true);
    // pi injects a live model switch into the running Pi process (via the bridge
    // inbox → setModel) and mirrors in-TUI /model picks back to model_override.
    expect(shouldShowModelPicker({ labels: { "omnigent.wrapper": "pi-native-ui" } })).toBe(true);
    // devin mirrors its live model into model_override (the executor types
    // /model when a routed model changes), like opencode/pi.
    expect(shouldShowModelPicker({ labels: { "omnigent.wrapper": "devin-native-ui" } })).toBe(true);
  });

  it("hides the picker for other wrappers and missing labels (fail closed)", () => {
    // WHY: a wrapper-looking string is not a resolved harness, and
    // pre-hydration rows still have no capability evidence.
    expect(shouldShowModelPicker({ labels: { "omnigent.wrapper": "codex-native" } })).toBe(false);
    expect(shouldShowModelPicker({ labels: {} })).toBe(false);
    expect(shouldShowModelPicker(null)).toBe(false);
    expect(shouldShowModelPicker(undefined)).toBe(false);
  });
});

describe("shouldShowEffortPicker", () => {
  it("shows effort controls only for claude-native sessions", () => {
    // WHY: delegates to supportsEffortControl — only claude-native exposes a
    // Web UI effort dial.
    expect(shouldShowEffortPicker({ labels: { "omnigent.wrapper": NATIVE } })).toBe(true);
  });

  it("shows effort controls for devin-native (effort is a model-variant suffix)", () => {
    // WHY: Devin has no --effort flag; effort is a model-variant suffix the
    // executor recombines and re-applies via /model, so the in-chat dial is live.
    expect(shouldShowEffortPicker({ labels: { "omnigent.wrapper": "devin-native-ui" } })).toBe(
      true,
    );
  });

  it("hides effort controls for other wrappers and missing labels", () => {
    // WHY: fail-closed — no label / non-native wrapper means no dial.
    expect(shouldShowEffortPicker({ labels: { "omnigent.wrapper": "codex-native" } })).toBe(false);
    expect(shouldShowEffortPicker(null)).toBe(false);
    expect(shouldShowEffortPicker(undefined)).toBe(false);
  });

  it("hides effort controls for cursor-native (model switch only, for now)", () => {
    // WHY: cursor effort lives on the /model picker's per-model "Tab to modify"
    // axis and a model switch resets it to that model's default, so a Web UI
    // dial would silently diverge from the TUI — dropped pending that fix.
    expect(shouldShowEffortPicker({ labels: { "omnigent.wrapper": "cursor-native-ui" } })).toBe(
      false,
    );
  });

  it("hides effort controls for opencode-native (model indicator only)", () => {
    // WHY: opencode surfaces its live model read-only (switching stays in the
    // opencode TUI); there is no Web UI effort dial for it.
    expect(shouldShowEffortPicker({ labels: { "omnigent.wrapper": "opencode-native-ui" } })).toBe(
      false,
    );
  });

  it("hides effort controls for kiro-native (model selection only)", () => {
    // WHY: kiro exposes only launch-time --model selection; its --effort knob is
    // not surfaced in the Web UI (deferred), so no effort dial.
    expect(shouldShowEffortPicker({ labels: { "omnigent.wrapper": "kiro-native-ui" } })).toBe(
      false,
    );
  });
});

describe("label-less native sessions (e.g. created before a harness rename)", () => {
  // The ACP-era Devin rows are the live case: no `omnigent.wrapper` label, while
  // the snapshot reports the canonical harness and the runner already resolves
  // them to native and gives them a pane. Without the harness fallback every
  // label-driven surface reads them as non-native.
  const oldDevin = { labels: {}, harness: "devin-native" };

  it("resolves a label-less devin session to Devin's picker", () => {
    expect(modelPickerKindForConv(oldDevin)).toBe("devin");
  });

  it("gives it Devin's per-model rungs rather than the base ladder", () => {
    const catalog = [
      {
        id: "swe-2",
        supportedReasoningEfforts: [{ reasoningEffort: "high" }, { reasoningEffort: "max" }],
      },
    ];
    expect(effortLevelsForConv(oldDevin, catalog, "swe-2")).toEqual(["high", "max"]);
  });

  it("still covers label-less codex, which this replaced a special case for", () => {
    expect(modelPickerKindForConv({ labels: {}, harness: "codex-native" })).toBe("codex");
  });

  it("does NOT resolve a label-less sub-agent child", () => {
    // A child owns no PTY and takes no input, so a native harness alone must not
    // earn it a picker — the ACP-era children carry no wrapper label either.
    expect(
      modelPickerKindForConv({
        labels: {},
        harness: "devin-native",
        parentSessionId: "conv_parent",
      }),
    ).toBeNull();
  });

  it("keeps an explicit label authoritative over the harness", () => {
    expect(
      modelPickerKindForConv({
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
        harness: "devin-native",
      }),
    ).toBe("claude");
  });

  it("still fails closed for a label-less non-native harness", () => {
    expect(modelPickerKindForConv({ labels: {}, harness: "openai-agents" })).toBeNull();
    expect(shouldShowModelPicker({ labels: {}, harness: "claude-sdk" })).toBe(false);
  });
});
