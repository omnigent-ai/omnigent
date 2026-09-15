// An MCP server's ``requestedSchema`` is answerable, not just approvable.
//
// The card had one schema shape it could collect — a lone ``answer`` enum,
// rendered as option buttons. Everything else fell through to Approve /
// Reject, so a server asking for a branch name got a yes carrying none of
// the fields it declared. These pin the reading of the schema and the shape
// of what goes back on the wire.

import { describe, expect, it } from "vitest";
import {
  type ElicitationDraft,
  enumLabel,
  isComplete,
  type SchemaField,
  schemaFields,
  toContent,
} from "./ElicitationSchemaForm";

const RELEASE_SCHEMA: Record<string, unknown> = {
  type: "object",
  properties: {
    branch: { type: "string", title: "Release branch" },
    notify: { type: "boolean", default: true },
    channel: { type: "string", enum: ["beta", "stable"] },
    holdback: { type: "integer" },
  },
  required: ["branch", "channel"],
};

describe("schemaFields", () => {
  it("reads every property in the order the server wrote them", () => {
    expect(schemaFields(RELEASE_SCHEMA).map((f) => f.name)).toEqual([
      "branch",
      "notify",
      "channel",
      "holdback",
    ]);
  });

  it("marks the required ones", () => {
    const required = schemaFields(RELEASE_SCHEMA)
      .filter((f) => f.required)
      .map((f) => f.name);
    expect(required).toEqual(["branch", "channel"]);
  });

  it("owns nothing when there is no schema at all", () => {
    // ``requestedSchema`` is optional on the wire; an absent one must not
    // throw where it is read, which includes the approve hotkey's guard.
    expect(schemaFields(undefined)).toEqual([]);
    expect(schemaFields(null)).toEqual([]);
  });

  it("owns nothing when the schema names no properties", () => {
    // A bare consent prompt. Approve / Reject is the right control, and a
    // form with no fields would be a worse one.
    expect(schemaFields({ type: "object" })).toEqual([]);
    expect(schemaFields({})).toEqual([]);
  });

  it("skips a property that is not an object", () => {
    // Malformed input from an outside server must not crash the card.
    const fields = schemaFields({ properties: { ok: { type: "string" }, bad: "nope" } });
    expect(fields.map((f) => f.name)).toEqual(["ok"]);
  });
});

describe("isComplete", () => {
  const fields: SchemaField[] = schemaFields(RELEASE_SCHEMA);

  it("blocks submit while a required field is blank", () => {
    // The server declared it; sending an accept without it is the bug this
    // whole form exists to stop.
    expect(isComplete(fields, { branch: "release/2.4", channel: "" })).toBe(false);
  });

  it("ignores blank optional fields", () => {
    expect(isComplete(fields, { branch: "release/2.4", channel: "beta" })).toBe(true);
  });

  it("treats false as an answered boolean", () => {
    // "No" is an answer to "should I notify?" — not an empty field.
    const boolOnly = schemaFields({
      properties: { force: { type: "boolean" } },
      required: ["force"],
    });
    expect(isComplete(boolOnly, { force: false })).toBe(true);
  });

  it("rejects whitespace as an answer", () => {
    expect(isComplete(fields, { branch: "   ", channel: "beta" })).toBe(false);
  });
});

describe("toContent", () => {
  const fields: SchemaField[] = schemaFields(RELEASE_SCHEMA);

  it("sends the values the person entered", () => {
    const answers: ElicitationDraft = {
      branch: "release/2.4",
      notify: true,
      channel: "stable",
      holdback: "10",
    };
    expect(toContent(fields, answers)).toEqual({
      branch: "release/2.4",
      notify: true,
      channel: "stable",
      holdback: 10,
    });
  });

  it("coerces a number field so the server gets a number", () => {
    // The input element hands back a string; the schema asked for an integer.
    expect(toContent(fields, { branch: "b", channel: "beta", holdback: "3" }).holdback).toBe(3);
  });

  it("omits an untouched optional field", () => {
    // Sending "" would read as answered-with-nothing rather than unanswered.
    const content = toContent(fields, { branch: "b", channel: "beta", holdback: "" });
    expect("holdback" in content).toBe(false);
  });

  it("keeps a false boolean", () => {
    const content = toContent(fields, { branch: "b", channel: "beta", notify: false });
    expect(content.notify).toBe(false);
  });
});

describe("values the server must never receive", () => {
  const numeric: SchemaField[] = schemaFields({
    properties: { holdback: { type: "integer" }, ratio: { type: "number" } },
    required: ["holdback"],
  });

  it("refuses a number that JSON would write as null", () => {
    // `Number("1e999")` is Infinity, and JSON.stringify turns that into null —
    // the server declared a number and would receive a null inside an accept.
    expect(isComplete(numeric, { holdback: "1e999" })).toBe(false);
    expect("holdback" in toContent(numeric, { holdback: "1e999" })).toBe(false);
  });

  it("refuses a fraction in an integer field", () => {
    expect(isComplete(numeric, { holdback: "3.5" })).toBe(false);
  });

  it("accepts a fraction in a number field", () => {
    expect(toContent(numeric, { holdback: "1", ratio: "0.25" }).ratio).toBe(0.25);
  });

  it("omits an unparseable numeric entry rather than sending text", () => {
    // Reachable through a non-conforming string `default` on a numeric
    // property. An accept carrying a type-invalid value is worse than one
    // carrying nothing: the server has already taken the accept path.
    expect("ratio" in toContent(numeric, { holdback: "1", ratio: "soon" })).toBe(false);
  });
});

describe("an untouched optional boolean", () => {
  const fields: SchemaField[] = schemaFields({ properties: { notify: { type: "boolean" } } });

  it("is not answered on the person's behalf", () => {
    // false and unanswered are different instructions: one turns something
    // off, the other leaves the server's own decision alone.
    const draft: ElicitationDraft = Object.fromEntries(fields.map((f) => [f.name, undefined]));
    expect("notify" in toContent(fields, draft)).toBe(false);
  });

  it("is sent once it is actually set to false", () => {
    expect(toContent(fields, { notify: false }).notify).toBe(false);
  });

  it("keeps a declared default", () => {
    const withDefault = schemaFields({
      properties: { notify: { type: "boolean", default: true } },
    });
    expect(toContent(withDefault, { notify: true }).notify).toBe(true);
  });
});

describe("enumNames", () => {
  const prop = { enum: ["c1", "c2"], enumNames: ["Beta channel", "Stable channel"] };

  it("shows the label the server supplied", () => {
    expect(enumLabel(prop, 0, "c1")).toBe("Beta channel");
  });

  it("falls back to the value when no label was given", () => {
    expect(enumLabel({ enum: ["c1"] }, 0, "c1")).toBe("c1");
    expect(enumLabel(prop, 5, "c9")).toBe("c9");
  });
});

describe("shapes the form must not claim", () => {
  it("leaves the ACP approval scopes on the option buttons", () => {
    // What `_executor_adapter._permission_card` emits for an ACP agent's own
    // approval scopes: `answer` alone, which the card renders as one button
    // per option. The form must not take that over — a list of scopes is a
    // pick-one, and a select would be a worse control than the buttons.
    const acpScopes = {
      type: "object",
      properties: {
        answer: {
          type: "string",
          enum: ["Allow", "Yes, allow `ls` commands (this session)", "Reject"],
        },
      },
      required: ["answer"],
    };

    // The card computes fields only when the option-button branch declines the
    // schema, and that branch owns any schema whose sole property is `answer`.
    expect(Object.keys(acpScopes.properties)).toEqual(["answer"]);
    expect(schemaFields(acpScopes).map((f) => f.name)).toEqual(["answer"]);
  });

  it("does claim `answer` once the server also asks for something else", () => {
    // Buttons here would submit the answer and silently drop the reason the
    // server declared required.
    const withReason = {
      properties: {
        answer: { type: "string", enum: ["yes", "no"] },
        reason: { type: "string" },
      },
      required: ["answer", "reason"],
    };
    expect(schemaFields(withReason).map((f) => f.name)).toEqual(["answer", "reason"]);
  });
});
