import { describe, expect, it } from "vitest";
import {
  harnessDisplayLabel,
  isSessionScopedDecision,
  routingExtras,
  routingExtrasFromWire,
  showsRoutingDecisionChip,
  subagentScopeLabel,
} from "./routingDecision";

describe("routingExtrasFromWire", () => {
  it("maps the snake_case wire fields to camelCase", () => {
    expect(
      routingExtrasFromWire({
        model: "databricks-claude-sonnet-5",
        harness: "codex-native",
        scope: "native_subagent",
        decision_id: "dec_1",
        raw_model: "gpt-5-6-sol",
        attempted_override: "databricks-claude-opus-4-8",
        router_source: "databricks-aigw",
      }),
    ).toEqual({
      harness: "codex-native",
      scope: "native_subagent",
      decisionId: "dec_1",
      rawModel: "gpt-5-6-sol",
      attemptedOverride: "databricks-claude-opus-4-8",
      routerSource: "databricks-aigw",
    });
  });

  // Kept a plain string on purpose: a source this build has never heard of must
  // survive the hop rather than be dropped.
  it("carries an unknown router source through verbatim", () => {
    expect(routingExtrasFromWire({ router_source: "some-future-router" })).toEqual({
      routerSource: "some-future-router",
    });
  });

  it.each([
    ["a blank string", ""],
    ["null", null],
    ["a non-string", 7],
  ] as const)("drops %s router_source rather than keying it undefined", (_case, routerSource) => {
    const extras = routingExtrasFromWire({ router_source: routerSource });
    expect(extras).toEqual({});
    expect("routerSource" in extras).toBe(false);
  });

  it("returns nothing for a legacy payload", () => {
    // Rows written before routing grew these fields must not gain keys —
    // the UI branches on presence to stay backward compatible.
    expect(routingExtrasFromWire({ model: "m", applied: true, rationale: "r" })).toEqual({});
  });

  it("drops blank values and unknown scopes", () => {
    expect(
      routingExtrasFromWire({ harness: "", scope: "galaxy", decision_id: null, raw_model: 7 }),
    ).toEqual({});
  });
});

describe("routingExtras", () => {
  it("copies only the set fields", () => {
    expect(routingExtras({ harness: "codex", scope: null, rawModel: "gpt-5-6-sol" })).toEqual({
      harness: "codex",
      rawModel: "gpt-5-6-sol",
    });
  });

  it("copies the router source through the camelCase hops", () => {
    expect(routingExtras({ routerSource: "oss-llm" })).toEqual({ routerSource: "oss-llm" });
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
  ] as const)("drops a %s router source rather than keying it", (_case, routerSource) => {
    const extras = routingExtras({ harness: "codex", routerSource });
    expect(extras).toEqual({ harness: "codex" });
    expect("routerSource" in extras).toBe(false);
  });
});

describe("subagentScopeLabel", () => {
  // Only the two sub-agent scopes get a badge, named after the agent when
  // there is one; session/turn (and a legacy absent scope) get none, or the
  // badge would invent a sub-agent the decision never covered.
  it.each([
    ["native_subagent", "researcher", "subagent: researcher"],
    ["child_session", "coder", "subagent: coder"],
    ["native_subagent", null, "subagent"],
    ["child_session", "  ", "subagent"],
    ["session", "x", null],
    ["turn", "x", null],
    [undefined, "x", null],
  ] as const)("scope %s + agent %s labels as %s", (scope, agent, expected) => {
    expect(subagentScopeLabel(scope, agent)).toBe(expected);
  });
});

describe("isSessionScopedDecision", () => {
  // The chip walker uses this to decide whether a decision belongs to the
  // session's own turn (and so pairs with a user message) or to a sub-agent it
  // spawned (which renders standalone). Legacy rows carry no scope and count
  // as the session's, or every pre-scope transcript would stop pairing.
  it.each([
    ["session", true],
    ["turn", true],
    [null, true],
    [undefined, true],
    ["native_subagent", false],
    ["child_session", false],
  ] as const)("scope %s is session-scoped: %s", (scope, expected) => {
    expect(isSessionScopedDecision(scope)).toBe(expected);
  });
});

describe("showsRoutingDecisionChip", () => {
  // The switch is two-state, so this gate mirrors behavior exactly: an unset or
  // "off" session's spawns are not routed, and a chip claiming a per-spawn pick
  // would be describing something that never happened. Everything else — the
  // session's own turn decisions and child-session decisions, which follow the
  // session's Smart Routing switch — renders regardless.
  it.each([
    ["native_subagent", "on", true],
    ["native_subagent", "off", false],
    ["native_subagent", null, false],
    ["native_subagent", undefined, false],
    ["child_session", null, true],
    ["session", null, true],
    ["turn", null, true],
    [null, null, true],
    [undefined, null, true],
  ] as const)("scope %s with override %s shows: %s", (scope, override, expected) => {
    expect(showsRoutingDecisionChip(scope, override)).toBe(expected);
  });
});

describe("harnessDisplayLabel", () => {
  it("shortens native harness ids", () => {
    expect(harnessDisplayLabel("claude-native")).toBe("claude");
    expect(harnessDisplayLabel("codex-native")).toBe("codex");
  });

  it("leaves SDK ids unchanged", () => {
    // A bundle agent's (Polly/Debby) SDK children carry no -native suffix.
    expect(harnessDisplayLabel("codex")).toBe("codex");
    expect(harnessDisplayLabel("claude-sdk")).toBe("claude-sdk");
    expect(harnessDisplayLabel("auto")).toBe("auto");
  });

  it("shortens only a trailing suffix", () => {
    expect(harnessDisplayLabel("native-brain")).toBe("native-brain");
  });

  it("returns null when the decision has no harness", () => {
    expect(harnessDisplayLabel(null)).toBeNull();
    expect(harnessDisplayLabel(undefined)).toBeNull();
    expect(harnessDisplayLabel("  ")).toBeNull();
  });
});
