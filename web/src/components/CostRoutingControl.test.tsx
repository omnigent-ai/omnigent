import { cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
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

  it("matches Claude Code and Codex in both flavours, plus the auto sentinel", () => {
    for (const harness of ["claude-native", "claude-sdk", "codex", "codex-native", "auto"]) {
      expect(isSubagentRoutingSession({ ...top, harness })).toBe(true);
    }
  });

  it("rejects harnesses with no native-subagent router", () => {
    expect(isSubagentRoutingSession({ ...top, harness: "pi" })).toBe(false);
    expect(isSubagentRoutingSession({ ...top, harness: "cursor-native" })).toBe(false);
    expect(isSubagentRoutingSession({ ...top, harness: null })).toBe(false);
  });

  it("rejects a child session even on a routable harness", () => {
    expect(
      isSubagentRoutingSession({
        agentName: "claude-native-ui",
        parentSessionId: "conv_parent987",
        harness: "claude-native",
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
