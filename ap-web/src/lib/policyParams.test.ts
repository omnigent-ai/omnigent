import { describe, it, expect } from "vitest";
import { coercePolicyParams } from "./policyParams";

describe("coercePolicyParams", () => {
  it("parses object-typed fields from a JSON object literal", () => {
    const result = coercePolicyParams(
      ["tool_points"],
      { tool_points: { type: "object" } },
      { tool_points: '{"sys_os_shell": 10}' },
    );
    // The whole point of the fix: the object reaches the server as a dict,
    // not the raw string that previously broke the create.
    expect(result).toEqual({
      ok: true,
      params: { tool_points: { sys_os_shell: 10 } },
    });
  });

  it("rejects malformed JSON in an object field instead of submitting it", () => {
    const result = coercePolicyParams(
      ["tool_points"],
      { tool_points: { type: "object" } },
      { tool_points: "{sys_os_shell: 10" },
    );
    expect(result.ok).toBe(false);
    // "valid JSON" is unique to the parse-failure branch (the non-object
    // branch says "JSON object"), so this proves we took the parse path.
    if (!result.ok) expect(result.error).toContain("valid JSON");
  });

  it("rejects a JSON array where an object is required", () => {
    const result = coercePolicyParams(
      ["tool_points"],
      { tool_points: { type: "object" } },
      { tool_points: "[1, 2]" },
    );
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("JSON object");
  });

  it("coerces integer, number, boolean, and array fields", () => {
    const result = coercePolicyParams(
      ["threshold", "ratio", "flag", "guarded_tools"],
      {
        threshold: { type: "integer" },
        ratio: { type: "number" },
        flag: { type: "boolean" },
        guarded_tools: { type: "array" },
      },
      {
        threshold: "20",
        ratio: "0.5",
        flag: "true",
        guarded_tools: "sys_os_shell, sys_os_write ,",
      },
    );
    expect(result).toEqual({
      ok: true,
      params: {
        threshold: 20,
        ratio: 0.5,
        flag: true,
        guarded_tools: ["sys_os_shell", "sys_os_write"],
      },
    });
  });

  it("passes string and unmapped types through verbatim", () => {
    const result = coercePolicyParams(
      ["state_key"],
      { state_key: { type: "string" } },
      { state_key: "risk_score" },
    );
    expect(result).toEqual({ ok: true, params: { state_key: "risk_score" } });
  });

  it("omits empty and unset fields", () => {
    const result = coercePolicyParams(
      ["a", "b"],
      { a: { type: "string" }, b: { type: "string" } },
      { a: "" },
    );
    // `a` is empty and `b` is unset, so neither reaches factory_params —
    // an empty field must not be sent as "" (which the server would reject).
    expect(result).toEqual({ ok: true, params: {} });
  });
});
