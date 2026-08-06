import { describe, expect, it } from "vitest";
import {
  SMART_ROUTING_ARMS,
  hostBacksHarnessWithGateway,
  smartRoutingDroppedMessage,
  smartRoutingSourceFor,
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

  it("names the arms the host doesn't back with the gateway", () => {
    expect(
      smartRoutingUnavailableReason({
        routingEnabled: true,
        wrappersRegistered: true,
        unreadyHarnesses: [],
        notGatewayBackedHarnesses: ["codex-native"],
      }),
    ).toEqual({ kind: "not-gateway-backed", harnesses: ["codex-native"] });
  });

  // Picking the harness is the external router's job. A deployment whose only
  // source is the built-in judge keeps per-harness routing and loses this row,
  // and it says so rather than blaming the host.
  it("reports the missing external router, gateway backing notwithstanding", () => {
    expect(
      smartRoutingUnavailableReason({
        routingEnabled: true,
        wrappersRegistered: true,
        unreadyHarnesses: [],
        notGatewayBackedHarnesses: [],
        externalRoutingAvailable: false,
      }),
    ).toEqual({ kind: "external-router-required" });
  });

  // Omitting the field keeps the pre-source answer (an older server that only
  // reports `smart_routing_enabled` must lose nothing); only an explicit false
  // takes the row away.
  it.each([
    [undefined, null],
    [true, null],
    [false, { kind: "external-router-required" }],
  ] as const)("externalRoutingAvailable %s yields %j", (externalRoutingAvailable, expected) => {
    expect(
      smartRoutingUnavailableReason({
        routingEnabled: true,
        wrappersRegistered: true,
        unreadyHarnesses: [],
        ...(externalRoutingAvailable === undefined ? {} : { externalRoutingAvailable }),
      }),
    ).toEqual(expected);
  });

  // Routing being off is the more fundamental cause, so it still wins.
  it("reports routing being off ahead of the missing external router", () => {
    expect(
      smartRoutingUnavailableReason({
        routingEnabled: false,
        wrappersRegistered: true,
        unreadyHarnesses: [],
        externalRoutingAvailable: false,
      }),
    ).toEqual({ kind: "routing-disabled" });
  });

  // ...and the missing router outranks anything about the host: no external
  // router means no fully-auto row however well the machine is set up.
  it.each([
    [
      "a wrapper agent is missing",
      { routingEnabled: true, wrappersRegistered: false, unreadyHarnesses: [] },
    ],
    [
      "an arm isn't ready",
      { routingEnabled: true, wrappersRegistered: true, unreadyHarnesses: ["claude-native"] },
    ],
  ] as const)("reports the missing external router ahead of %s", (_case, inputs) => {
    expect(
      smartRoutingUnavailableReason({
        ...inputs,
        notGatewayBackedHarnesses: ["codex-native"],
        externalRoutingAvailable: false,
      }),
    ).toEqual({ kind: "external-router-required" });
  });

  // The built-in judge does not cover an off-gateway arm here — it can't pick
  // the harness — so the gateway cause stands even with the judge configured.
  it("still reports an off-gateway arm with the external router present", () => {
    expect(
      smartRoutingUnavailableReason({
        routingEnabled: true,
        wrappersRegistered: true,
        unreadyHarnesses: [],
        notGatewayBackedHarnesses: SMART_ROUTING_ARMS,
        externalRoutingAvailable: true,
      }),
    ).toEqual({ kind: "not-gateway-backed", harnesses: ["claude-native", "codex-native"] });
  });

  // An arm that isn't installed can't have an inference config to judge, so
  // "set up the CLI" is the actionable line.
  it("reports an unready arm ahead of a non-gateway-backed one", () => {
    expect(
      smartRoutingUnavailableReason({
        routingEnabled: true,
        wrappersRegistered: true,
        unreadyHarnesses: ["claude-native"],
        notGatewayBackedHarnesses: ["codex-native"],
      }),
    ).toEqual({ kind: "harnesses-unready", harnesses: ["claude-native"] });
  });

  it("reports routing being off ahead of the gateway backing", () => {
    expect(
      smartRoutingUnavailableReason({
        routingEnabled: false,
        wrappersRegistered: true,
        unreadyHarnesses: [],
        notGatewayBackedHarnesses: SMART_ROUTING_ARMS,
      }),
    ).toEqual({ kind: "routing-disabled" });
  });

  it("is null when the gateway list is empty or omitted", () => {
    const inputs = { routingEnabled: true, wrappersRegistered: true, unreadyHarnesses: [] };
    expect(smartRoutingUnavailableReason({ ...inputs, notGatewayBackedHarnesses: [] })).toBeNull();
    expect(smartRoutingUnavailableReason(inputs)).toBeNull();
  });
});

describe("smartRoutingSourceFor", () => {
  // The full truth table of the three booleans. The external AI-Gateway router
  // wins whenever it can answer (the family is on the gateway); the built-in
  // judge is the fallback and needs no gateway backing at all.
  it.each([
    [true, true, true, "databricks-aigw"],
    [true, false, true, "databricks-aigw"],
    [true, true, false, "oss-llm"],
    [true, false, false, null],
    [false, true, true, "oss-llm"],
    [false, true, false, "oss-llm"],
    [false, false, true, null],
    [false, false, false, null],
  ] as const)(
    "external %s + oss %s + gatewayBacked %s routes via %s",
    (externalConfigured, ossConfigured, gatewayBacked, expected) => {
      expect(smartRoutingSourceFor({ externalConfigured, ossConfigured, gatewayBacked })).toBe(
        expected,
      );
    },
  );
});

describe("hostBacksHarnessWithGateway", () => {
  const hostWith = (gateway: Record<string, boolean> | null | undefined) => ({
    gateway_inference: gateway,
  });

  it("withholds only on an explicit false", () => {
    const testHost = hostWith({ "claude-native": true, "codex-native": false });
    expect(hostBacksHarnessWithGateway(testHost, "claude-native")).toBe(true);
    expect(hostBacksHarnessWithGateway(testHost, "codex-native")).toBe(false);
  });

  it("reads unknown as backed", () => {
    // Older host / server (no map), a spelling the host didn't report, and a
    // sandbox with no host row at all.
    expect(hostBacksHarnessWithGateway(hostWith(null), "codex-native")).toBe(true);
    expect(hostBacksHarnessWithGateway(hostWith(undefined), "codex-native")).toBe(true);
    expect(hostBacksHarnessWithGateway({}, "codex-native")).toBe(true);
    expect(hostBacksHarnessWithGateway(hostWith({ codex: false }), "codex-native")).toBe(true);
    expect(hostBacksHarnessWithGateway(undefined, "codex-native")).toBe(true);
    expect(hostBacksHarnessWithGateway(null, "codex-native")).toBe(true);
  });
});

describe("smartRoutingDroppedMessage", () => {
  const context = { hostName: "machine-2", fallbackAgentName: "Claude Code" };

  it("blames the server flag, not the host, when routing is off", () => {
    const message = smartRoutingDroppedMessage({ kind: "routing-disabled" }, context);
    expect(message).toBe("Smart Routing is turned off on this server — switched to Claude Code.");
    expect(message).not.toContain("machine-2");
  });

  // Judge-only: blame the server's router set, not the host — the machine is
  // fine, and "set up your CLI" would send the user down the wrong path.
  it("blames the server's router set when only the built-in judge is configured", () => {
    const message = smartRoutingDroppedMessage({ kind: "external-router-required" }, context);
    expect(message).toBe(
      "Smart Routing across harnesses needs the workspace AI gateway router on this server — switched to Claude Code.",
    );
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

  it("names the arm that isn't on the workspace AI gateway", () => {
    expect(
      smartRoutingDroppedMessage(
        { kind: "not-gateway-backed", harnesses: ["codex-native"] },
        context,
      ),
    ).toBe(
      "Smart Routing needs Codex running on the workspace AI gateway on machine-2 — switched to Claude Code.",
    );
  });

  it("names both arms when neither is on the workspace AI gateway", () => {
    expect(
      smartRoutingDroppedMessage(
        { kind: "not-gateway-backed", harnesses: ["claude-native", "codex-native"] },
        context,
      ),
    ).toBe(
      "Smart Routing needs Claude Code and Codex running on the workspace AI gateway on machine-2 — switched to Claude Code.",
    );
  });

  it("drops the host clause for a sandbox and falls back on the agent name", () => {
    expect(
      smartRoutingDroppedMessage({ kind: "harnesses-unready", harnesses: ["codex-native"] }),
    ).toBe("Smart Routing needs Codex ready — switched to the default agent.");
  });
});
