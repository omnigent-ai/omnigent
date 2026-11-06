import { describe, expect, it } from "vitest";
import {
  LABEL_KEY_MAX_LENGTH,
  LABEL_VALUE_MAX_LENGTH,
  isInternalLabelKey,
  labelRecordFromUnknown,
  labelValidationError,
  labelsEqual,
  newSessionLabels,
  projectDefaultLabels,
  userLabelEntries,
} from "./sessionLabels";

describe("isInternalLabelKey", () => {
  it.each(["omnigent.pinned", "omnigent.wrapper", "cost_control.plan", "omni_project"])(
    "treats %s as internal",
    (key) => expect(isInternalLabelKey(key)).toBe(true),
  );

  it.each(["team", "omnigent", "omni_project_name", "cost-center"])(
    "treats %s as a user label",
    (key) => expect(isInternalLabelKey(key)).toBe(false),
  );
});

describe("userLabelEntries", () => {
  it("drops internal keys and sorts by key", () => {
    expect(
      userLabelEntries({
        team: "ml",
        "omnigent.pinned": "1",
        env: "prod",
        omni_project: "Sprint",
        "cost_control.plan": "{}",
      }),
    ).toEqual([
      ["env", "prod"],
      ["team", "ml"],
    ]);
  });

  it("returns nothing for a missing map", () => {
    expect(userLabelEntries(undefined)).toEqual([]);
  });
});

describe("labelValidationError", () => {
  it("accepts a key at the column width, a value at its width, and an empty value", () => {
    expect(
      labelValidationError("k".repeat(LABEL_KEY_MAX_LENGTH), "v".repeat(LABEL_VALUE_MAX_LENGTH)),
    ).toBeNull();
    expect(labelValidationError("tag", "")).toBeNull();
  });

  it.each([
    ["", "x", "Key is required."],
    ["k".repeat(LABEL_KEY_MAX_LENGTH + 1), "x", "Key must be 128 characters or fewer."],
    ["team", "v".repeat(LABEL_VALUE_MAX_LENGTH + 1), "Value must be 256 characters or fewer."],
    ["omnigent.pinned", "1", '"omnigent.pinned" is reserved for Omnigent.'],
  ])("rejects key %j", (key, value, message) => {
    expect(labelValidationError(key, value)).toBe(message);
  });
});

describe("labelRecordFromUnknown", () => {
  it("keeps string values and ignores malformed input", () => {
    expect(labelRecordFromUnknown({ team: "ml", count: 3, nested: { a: "b" } })).toEqual({
      team: "ml",
    });
    expect(labelRecordFromUnknown("team=ml")).toEqual({});
    expect(labelRecordFromUnknown(["team"])).toEqual({});
    expect(labelRecordFromUnknown(null)).toEqual({});
  });
});

describe("projectDefaultLabels", () => {
  it("keeps the labels the server would stamp and drops the rest", () => {
    expect(
      projectDefaultLabels({
        labels: { team: "ml", "omnigent.pinned": "1", omni_project: "x", count: 3 },
      }),
    ).toEqual({ team: "ml" });
    expect(projectDefaultLabels(undefined)).toEqual({});
  });
});

describe("newSessionLabels", () => {
  it("layers this session's edits over the project defaults", () => {
    expect(newSessionLabels({ team: "ml" }, { env: "dev", team: "infra" })).toEqual({
      labels: { team: "infra", env: "dev" },
      requestLabels: { team: "infra", env: "dev" },
    });
  });

  it("sends a removed project default as null so the server leaves it off", () => {
    expect(newSessionLabels({ team: "ml", env: "dev" }, { team: null })).toEqual({
      labels: { env: "dev" },
      requestLabels: { team: null, env: "dev" },
    });
  });
});

describe("labelsEqual", () => {
  it("compares entries regardless of order", () => {
    expect(labelsEqual({ a: "1", b: "2" }, { b: "2", a: "1" })).toBe(true);
    expect(labelsEqual({ a: "1" }, { a: "2" })).toBe(false);
    expect(labelsEqual({ a: "1" }, { a: "1", b: "2" })).toBe(false);
  });
});
