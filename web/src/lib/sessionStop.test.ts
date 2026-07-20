// Unit tests for `isSessionStoppable` — each case pins one gate branch.

import { describe, expect, it } from "vitest";
import { isSessionStoppable } from "./sessionStop";

describe("isSessionStoppable", () => {
  it("is true for a CLI-launched claude-native session (no host)", () => {
    // Native tmux kill works without a host.
    expect(
      isSessionStoppable({
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
        hostId: null,
        runnerId: null,
      }),
    ).toBe(true);
  });

  it("is true for a host-spawned session of any harness (no wrapper label)", () => {
    // Core new behavior: host binding, no wrapper label. Fails if the
    // gate is still claude-native-only.
    expect(
      isSessionStoppable({
        labels: {},
        hostId: "host_a1b2",
        runnerId: "runner_token_abc",
      }),
    ).toBe(true);
  });

  it("is true for a host-spawned codex-native session (via the host kill path)", () => {
    // Qualifies via host-spawning, not the wrapper branch.
    expect(
      isSessionStoppable({
        labels: { "omnigent.wrapper": "codex-native-ui" },
        hostId: "host_a1b2",
        runnerId: "runner_token_xyz",
      }),
    ).toBe(true);
  });

  it("is false for a local CLI in-process runner (runner_id but no host_id)", () => {
    // runner_id but no host_id → no kill path. Fails if the gate keyed
    // off runner_id alone.
    expect(
      isSessionStoppable({
        labels: {},
        hostId: null,
        runnerId: "runner_local_123",
      }),
    ).toBe(false);
  });

  it("is false when host_id is set but runner_id is missing (and vice versa)", () => {
    // Both fields required to target a host kill.
    expect(isSessionStoppable({ labels: {}, hostId: "host_a1b2", runnerId: null })).toBe(false);
    expect(isSessionStoppable({ labels: {}, hostId: null, runnerId: "runner_token_abc" })).toBe(
      false,
    );
  });

  it("is false for a claude-native sub-agent (parent owns the kill)", () => {
    // `-subagent` wrapper isn't the stoppable value; parent owns the kill.
    expect(
      isSessionStoppable({
        labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" },
        hostId: null,
        runnerId: null,
      }),
    ).toBe(false);
  });

  it("is false for a plain session with no labels and no runner", () => {
    // No label, no host → hidden; undefined labels must not throw.
    expect(isSessionStoppable({ labels: undefined, hostId: null, runnerId: null })).toBe(false);
  });
});
