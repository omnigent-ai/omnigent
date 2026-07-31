import { describe, expect, it } from "vitest";
import {
  SMART_ROUTING_ARMS,
  smartRoutingDroppedMessage,
  smartRoutingUnavailableReason,
} from "./smartRoutingAvailability";

describe("smartRoutingUnavailableReason", () => {
  it("is null when routing is on, both wrappers exist, and both arms are ready", () => {
    expect(
      smartRoutingUnavailableReason({
        routingEnabled: true,
        wrappersRegistered: true,
        unreadyHarnesses: [],
      }),
    ).toBeNull();
  });

  // Most-fundamental cause wins: with routing off, the host's CLIs are moot.
  it("reports routing being off ahead of the host's readiness", () => {
    expect(
      smartRoutingUnavailableReason({
        routingEnabled: false,
        wrappersRegistered: false,
        unreadyHarnesses: ["codex-native"],
      }),
    ).toEqual({ kind: "routing-disabled" });
  });

  it("reports missing wrappers ahead of the host's readiness", () => {
    expect(
      smartRoutingUnavailableReason({
        routingEnabled: true,
        wrappersRegistered: false,
        unreadyHarnesses: ["codex-native"],
      }),
    ).toEqual({ kind: "wrappers-missing" });
  });

  it("names the arms that aren't ready", () => {
    expect(
      smartRoutingUnavailableReason({
        routingEnabled: true,
        wrappersRegistered: true,
        unreadyHarnesses: SMART_ROUTING_ARMS,
      }),
    ).toEqual({ kind: "harnesses-unready", harnesses: ["claude-native", "codex-native"] });
  });
});

describe("smartRoutingDroppedMessage", () => {
  const context = { hostName: "machine-2", fallbackAgentName: "Claude Code" };

  it("blames the server flag, not the host, when routing is off", () => {
    const message = smartRoutingDroppedMessage({ kind: "routing-disabled" }, context);
    expect(message).toBe("Smart Routing is turned off on this server — switched to Claude Code.");
    expect(message).not.toContain("machine-2");
  });

  it("blames registration when a wrapper agent is missing", () => {
    expect(smartRoutingDroppedMessage({ kind: "wrappers-missing" }, context)).toBe(
      "Smart Routing needs the Claude Code and Codex agents registered on this server — switched to Claude Code.",
    );
  });

  it("names only the arm that isn't ready on the host", () => {
    expect(
      smartRoutingDroppedMessage(
        { kind: "harnesses-unready", harnesses: ["codex-native"] },
        context,
      ),
    ).toBe("Smart Routing needs Codex ready on machine-2 — switched to Claude Code.");
  });

  it("names both arms when both are missing", () => {
    expect(
      smartRoutingDroppedMessage(
        { kind: "harnesses-unready", harnesses: ["claude-native", "codex-native"] },
        context,
      ),
    ).toBe(
      "Smart Routing needs Claude Code and Codex ready on machine-2 — switched to Claude Code.",
    );
  });

  it("drops the host clause for a sandbox and falls back on the agent name", () => {
    expect(
      smartRoutingDroppedMessage({ kind: "harnesses-unready", harnesses: ["codex-native"] }),
    ).toBe("Smart Routing needs Codex ready — switched to the default agent.");
  });
});
