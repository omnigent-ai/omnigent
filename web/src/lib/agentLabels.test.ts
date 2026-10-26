import { describe, expect, it } from "vitest";

import { liveSteeringFromCatalog, readsFollowUpsOnNextTurn } from "./agentLabels";

describe("liveSteeringFromCatalog", () => {
  it("keeps the string spellings the server lists", () => {
    expect(liveSteeringFromCatalog(["codex", 7, null, "pi"])).toEqual(new Set(["codex", "pi"]));
  });

  it.each([undefined, null, "codex", { codex: true }])(
    "is null when the catalog has no list (%s)",
    (value) => {
      expect(liveSteeringFromCatalog(value)).toBeNull();
    },
  );
});

describe("readsFollowUpsOnNextTurn", () => {
  const steering = new Set(["codex", "pi"]);

  it("holds follow-ups for an SDK harness that does not steer its running turn", () => {
    expect(readsFollowUpsOnNextTurn("openai-agents", steering, false)).toBe(true);
  });

  it.each<[string, string | null, ReadonlySet<string> | null, boolean]>([
    ["a steering harness", "codex", steering, false],
    ["a native terminal session", "claude-native", steering, true],
    ["an unknown session harness", null, steering, false],
    ["a catalog without the list", "openai-agents", null, false],
  ])("keeps arrival order for %s", (_case, harness, liveSteering, isNative) => {
    expect(readsFollowUpsOnNextTurn(harness, liveSteering, isNative)).toBe(false);
  });
});
