import { describe, expect, it } from "vitest";

import {
  parentIdFromLabels,
  setSessionHost,
  getSessionHost,
  observeSessionHostProjection,
} from "./sessionHost";

describe("parentIdFromLabels", () => {
  it("reads the explicit side-chat source id", () => {
    expect(
      parentIdFromLabels({
        "omnigent.side_chat": "1",
        "omnigent.side_chat.source_id": "parent_1",
      }),
    ).toBe("parent_1");
  });

  it("falls back to the fork source id for a side chat without an explicit source", () => {
    expect(
      parentIdFromLabels({ "omnigent.side_chat": "1", "omnigent.fork.source_id": "parent_2" }),
    ).toBe("parent_2");
  });

  it("ignores a fork source id when the session is not a side chat", () => {
    expect(parentIdFromLabels({ "omnigent.fork.source_id": "parent_3" })).toBeNull();
  });

  it("returns null for an independent session or missing labels", () => {
    expect(parentIdFromLabels({})).toBeNull();
    expect(parentIdFromLabels(undefined)).toBeNull();
  });
});

describe("routing binding ownership", () => {
  const observation = {
    runner_id: "B",
    binding_epoch: 2,
    revision: 5,
    observation_id: "new",
    status: "running",
    response_id: "new_turn",
    error_key: null,
  };

  it("learns a binding from status before accepting its older same-binding metadata", () => {
    setSessionHost("routing_live", "host_A", {
      observation: { ...observation, runner_id: "A", binding_epoch: 1 },
    });
    observeSessionHostProjection("routing_live", { observation });
    expect(getSessionHost("routing_live")).toBe(null);
    expect(
      setSessionHost("routing_live", "host_A", {
        observation: { ...observation, runner_id: "A", binding_epoch: 1 },
      }),
    ).toBe(false);
    expect(
      setSessionHost("routing_live", "host_B", { observation: { ...observation, revision: 4 } }),
    ).toBe(true);
    expect(getSessionHost("routing_live")).toBe("host_B");
  });

  it("drops an unversioned address when the first versioned status arrives", () => {
    setSessionHost("routing_first_version", "host_A", {});
    observeSessionHostProjection("routing_first_version", { observation });
    expect(getSessionHost("routing_first_version")).toBe(null);
    expect(setSessionHost("routing_first_version", "host_A", {})).toBe(false);
    expect(
      setSessionHost("routing_first_version", "host_B", {
        observation: { ...observation, revision: 4 },
      }),
    ).toBe(true);
    expect(getSessionHost("routing_first_version")).toBe("host_B");
  });

  it("rejects a legacy snapshot after a version but accepts a newer explicit clear", () => {
    setSessionHost("routing_clear", "host_B", { observation });
    expect(setSessionHost("routing_clear", "host_A", {})).toBe(false);
    expect(getSessionHost("routing_clear")).toBe("host_B");
    expect(
      setSessionHost("routing_clear", null, {
        observation: { ...observation, runner_id: null, binding_epoch: 3 },
      }),
    ).toBe(true);
    expect(getSessionHost("routing_clear")).toBe(null);
  });

  it("keeps unversioned routing compatible until a binding version is learned", () => {
    expect(setSessionHost("routing_legacy", "host_A", {})).toBe(true);
    expect(setSessionHost("routing_legacy", "host_B", {})).toBe(true);
    expect(getSessionHost("routing_legacy")).toBe("host_B");
  });
});
