import { describe, expect, it } from "vitest";
import {
  acceptSessionProjection,
  parseSessionObservation,
  type SessionObservation,
} from "./sessionObservation";

const active: SessionObservation = {
  runner_id: "runner_A",
  binding_epoch: 2,
  revision: 7,
  observation_id: "active_observation",
  status: "running",
  response_id: "active_turn",
  error_key: null,
};

describe("session observation ordering", () => {
  it.each([
    {
      name: "new binding",
      incoming: { ...active, binding_epoch: 3, runner_id: "runner_B" },
      accepted: true,
    },
    { name: "old binding", incoming: { ...active, binding_epoch: 1 }, accepted: false },
    {
      name: "new turn",
      incoming: { ...active, revision: 8, response_id: "next_turn" },
      accepted: true,
    },
    { name: "old turn", incoming: { ...active, revision: 6 }, accepted: false },
    { name: "duplicate", incoming: active, accepted: true },
    {
      name: "conflicting equal revision",
      incoming: { ...active, response_id: "different_turn" },
      accepted: false,
    },
    { name: "legacy", incoming: undefined, accepted: false },
  ])("orders $name without using arrival time", ({ incoming, accepted }) => {
    expect(acceptSessionProjection(incoming, null, active, null)).toBe(accepted);
  });

  it("allows a current owned failure, preserving it until a newer normal observation", () => {
    expect(acceptSessionProjection(active, "outage", active, null)).toBe(true);
    expect(acceptSessionProjection(active, null, active, "outage")).toBe(false);
    expect(
      acceptSessionProjection({ ...active, response_id: "other" }, "outage", active, null),
    ).toBe(false);
    expect(acceptSessionProjection(active, "different_outage", active, "outage")).toBe(false);
    expect(acceptSessionProjection({ ...active, revision: 8 }, null, active, "outage")).toBe(true);
  });

  it("retains legacy behavior until a real version is learned", () => {
    const legacy = parseSessionObservation({ binding_epoch: 0, revision: 0 });
    expect(legacy?.runner_id).toBe(null);
    expect(acceptSessionProjection(undefined, null, legacy, null)).toBe(true);
    expect(acceptSessionProjection(active, null, legacy, null)).toBe(true);
  });

  it.each([true, -1, 1.5, "7", Number.NaN])("rejects malformed numeric revision %s", (revision) => {
    expect(parseSessionObservation({ ...active, revision })).toBe(undefined);
  });
});
