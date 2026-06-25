import { describe, expect, it } from "vitest";
import {
  effortLevelsForConv,
  isModelImplicitlySelected,
  shouldShowEffortPicker,
  shouldShowModelPicker,
} from "./ChatPage";

// These pin the label-driven composer capability gates (effort levels, model
// picker, effort picker) and the model-row implicit-selection match. They
// fail closed on missing labels, so a refactor that loosens the gate would
// expose model/effort controls on sessions that can't honor mid-session
// overrides (codex-native pins its model at launch; non-claude wrappers have
// no Web UI effort dial).

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
  });

  it("hides the picker for other wrappers and missing labels (fail closed)", () => {
    // WHY: a loosened gate would pop a non-functional picker on codex-native
    // (model pinned at launch) and on pre-hydration rows.
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
});

describe("isModelImplicitlySelected", () => {
  it("matches a tier alias against the bound full spec by suffix", () => {
    // WHY: with no explicit override, the row whose alias is the suffix of the
    // bound spec ("anthropic/claude-opus-4-8" → "opus" via includes) lights up
    // so the user sees which model is actually running.
    expect(isModelImplicitlySelected("opus", "anthropic/claude-opus-4-8")).toBe(true);
  });

  it("matches an exact spec equality", () => {
    // WHY: the identity branch — a fully-qualified id that equals the bound
    // spec is selected.
    expect(isModelImplicitlySelected("databricks-gpt-5-4", "databricks-gpt-5-4")).toBe(true);
  });

  it("matches a path-suffix without a substring false-positive elsewhere", () => {
    // WHY: the endsWith("/id") branch — the alias is the trailing path segment.
    expect(isModelImplicitlySelected("sonnet", "anthropic/claude-sonnet")).toBe(true);
  });

  it("returns false when no model is bound (null spec)", () => {
    // WHY: nothing bound → nothing implicitly selected; guards the early null
    // return so we don't highlight a row on a fresh session.
    expect(isModelImplicitlySelected("opus", null)).toBe(false);
  });

  it("returns false when the alias appears nowhere in the bound spec", () => {
    // WHY: a non-matching alias must not light up — otherwise two rows could
    // read as selected.
    expect(isModelImplicitlySelected("opus", "anthropic/claude-sonnet-4")).toBe(false);
  });
});
