import { describe, expect, it } from "vitest";
import {
  isSessionScopedDecision,
  routingExtras,
  routingExtrasFromWire,
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
      }),
    ).toEqual({
      harness: "codex-native",
      scope: "native_subagent",
      decisionId: "dec_1",
      rawModel: "gpt-5-6-sol",
      attemptedOverride: "databricks-claude-opus-4-8",
    });
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
