import { describe, it, expect } from "vitest";
import {
  UI_MODE_LABEL_KEY,
  UI_MODE_TERMINAL_VALUE,
  WRAPPER_LABEL_KEY,
  nativeCodingAgentForHarness,
  nativeWrapperLabelsForAgent,
} from "./nativeCodingAgents";

describe("nativeCodingAgentForHarness", () => {
  it("resolves the canonical pi-native harness", () => {
    expect(nativeCodingAgentForHarness("pi-native")?.key).toBe("pi");
  });

  it("resolves the canonical opencode-native harness", () => {
    expect(nativeCodingAgentForHarness("opencode-native")?.key).toBe("opencode");
  });

  it("resolves the canonical qwen-native harness", () => {
    const agent = nativeCodingAgentForHarness("qwen-native");
    expect(agent?.key).toBe("qwen");
    expect(agent?.displayName).toBe("Qwen Code");
  });

  it("folds the reversed native-qwen alias to the qwen-native spec", () => {
    expect(nativeCodingAgentForHarness("native-qwen")).toBe(
      nativeCodingAgentForHarness("qwen-native"),
    );
  });

  // The server's harness_kind returns the raw executor.config.harness, so a
  // `native-pi` agent must fold to the same spec — else fork/switch into it
  // would miss the terminal-first wrapper labels and render as chat.
  it("folds the reversed native-pi alias to the pi-native spec", () => {
    expect(nativeCodingAgentForHarness("native-pi")).toBe(nativeCodingAgentForHarness("pi-native"));
  });

  it("resolves the canonical antigravity-native harness", () => {
    expect(nativeCodingAgentForHarness("antigravity-native")?.key).toBe("antigravity");
  });

  // Same reversed-alias contract as native-pi: `native-antigravity` must
  // fold to the canonical antigravity-native spec.
  it("folds the reversed native-antigravity alias to the antigravity-native spec", () => {
    expect(nativeCodingAgentForHarness("native-antigravity")).toBe(
      nativeCodingAgentForHarness("antigravity-native"),
    );
  });

  it("leaves unknown / non-native harnesses unresolved", () => {
    expect(nativeCodingAgentForHarness("claude-sdk")).toBeUndefined();
    // The in-process Antigravity SDK harness is not a native CLI wrapper.
    expect(nativeCodingAgentForHarness("antigravity")).toBeUndefined();
    expect(nativeCodingAgentForHarness(null)).toBeUndefined();
    expect(nativeCodingAgentForHarness(undefined)).toBeUndefined();
  });
});

describe("nativeWrapperLabelsForAgent", () => {
  it("stamps terminal-first labels for a native-pi agent", () => {
    expect(nativeWrapperLabelsForAgent({ name: "my-pi", harness: "native-pi" })).toEqual({
      [UI_MODE_LABEL_KEY]: UI_MODE_TERMINAL_VALUE,
      [WRAPPER_LABEL_KEY]: "pi-native-ui",
    });
  });

  it("stamps terminal-first labels for a native-antigravity agent", () => {
    expect(nativeWrapperLabelsForAgent({ name: "my-agy", harness: "native-antigravity" })).toEqual({
      [UI_MODE_LABEL_KEY]: UI_MODE_TERMINAL_VALUE,
      [WRAPPER_LABEL_KEY]: "antigravity-native-ui",
    });
  });

  it("stamps terminal-first labels for an opencode-native agent", () => {
    expect(
      nativeWrapperLabelsForAgent({ name: "my-opencode", harness: "opencode-native" }),
    ).toEqual({
      [UI_MODE_LABEL_KEY]: UI_MODE_TERMINAL_VALUE,
      [WRAPPER_LABEL_KEY]: "opencode-native-ui",
    });
  });
});
