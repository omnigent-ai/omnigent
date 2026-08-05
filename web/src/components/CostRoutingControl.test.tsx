import { cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  AUTO_HARNESS_LABEL_KEY,
  isCostRoutingSession,
  isSubagentRoutingSession,
  shortModelName,
} from "./CostRoutingControl";

afterEach(cleanup);

describe("isCostRoutingSession", () => {
  it("matches any top-level session with an agent name", () => {
    expect(isCostRoutingSession({ agentName: "polly", parentSessionId: null })).toBe(true);
    expect(isCostRoutingSession({ agentName: "debby", parentSessionId: null })).toBe(true);
  });

  it("rejects a child session", () => {
    expect(isCostRoutingSession({ agentName: "polly", parentSessionId: "conv_parent987" })).toBe(
      false,
    );
  });

  it("rejects a session with no agent name", () => {
    expect(isCostRoutingSession({ agentName: null, parentSessionId: null })).toBe(false);
  });

  it("rejects a missing session", () => {
    expect(isCostRoutingSession(null)).toBe(false);
    expect(isCostRoutingSession(undefined)).toBe(false);
  });
});

describe("isSubagentRoutingSession", () => {
  const top = { agentName: "claude-native-ui", parentSessionId: null };

  it("matches a routed Claude session and any auto-harness session", () => {
    expect(
      isSubagentRoutingSession({
        ...top,
        harness: "claude-native",
        costControlModeOverride: "on",
      }),
    ).toBe(true);
    // Auto-harness may land on either family, and both install the apparatus.
    expect(isSubagentRoutingSession({ ...top, harness: "auto" })).toBe(true);
    for (const harness of ["claude-native", "codex-native"]) {
      expect(
        isSubagentRoutingSession({
          ...top,
          harness,
          labels: { [AUTO_HARNESS_LABEL_KEY]: "1" },
        }),
      ).toBe(true);
    }
  });

  it("rejects a session whose spawn routing is fixed off at launch", () => {
    // Pinned codex: spawn routing there needs the generated hooks and the
    // routed-spawn pre-approvals, which only an auto-harness launch installs.
    expect(
      isSubagentRoutingSession({
        ...top,
        harness: "codex-native",
        costControlModeOverride: "on",
      }),
    ).toBe(false);
    // A plain native session of either family: the apparatus is stamped at
    // create, so flipping the switch afterwards would change nothing.
    for (const harness of ["claude-native", "codex-native"]) {
      expect(isSubagentRoutingSession({ ...top, harness })).toBe(false);
      expect(isSubagentRoutingSession({ ...top, harness, costControlModeOverride: "off" })).toBe(
        false,
      );
    }
  });

  it("matches non-native sessions whatever their harness (spawns go through create)", () => {
    // An SDK/bundle agent spawns children through the session-create path, which
    // routes off this switch regardless of the harness the parent runs.
    expect(isSubagentRoutingSession({ ...top, harness: "pi" })).toBe(true);
    expect(isSubagentRoutingSession({ ...top, harness: "openai-agents" })).toBe(true);
    expect(isSubagentRoutingSession({ ...top, harness: "not-a-real-harness" })).toBe(true);
    expect(isSubagentRoutingSession({ ...top, harness: null })).toBe(true);
  });

  it("rejects native wrappers with no native-subagent router", () => {
    // Native terminal CLIs spawn in-harness through the hook, which only
    // Claude/Codex implement — the knob would be inert elsewhere.
    expect(isSubagentRoutingSession({ ...top, harness: "cursor-native" })).toBe(false);
    expect(isSubagentRoutingSession({ ...top, harness: "pi-native" })).toBe(false);
    expect(
      isSubagentRoutingSession({
        ...top,
        harness: "pi",
        labels: { "omnigent.wrapper": "cursor-native-ui" },
      }),
    ).toBe(false);
  });

  it("rejects a child session even on a routable harness", () => {
    expect(
      isSubagentRoutingSession({
        agentName: "claude-native-ui",
        parentSessionId: "conv_parent987",
        harness: "claude-native",
        costControlModeOverride: "on",
      }),
    ).toBe(false);
  });

  it("rejects a missing session", () => {
    expect(isSubagentRoutingSession(null)).toBe(false);
    expect(isSubagentRoutingSession(undefined)).toBe(false);
  });
});

describe("shortModelName", () => {
  it("collapses Claude ids to their family token", () => {
    expect(shortModelName("databricks-claude-haiku-4-5")).toBe("haiku");
    expect(shortModelName("databricks-claude-sonnet-4-6")).toBe("sonnet");
    expect(shortModelName("claude-opus-4-7")).toBe("opus");
  });

  it("strips the databricks- prefix from non-Claude ids", () => {
    expect(shortModelName("databricks-gpt-5-4-mini")).toBe("gpt-5-4-mini");
  });

  it("passes unrecognized ids through unchanged (fallback to the id)", () => {
    expect(shortModelName("gpt-5.4")).toBe("gpt-5.4");
  });
});
