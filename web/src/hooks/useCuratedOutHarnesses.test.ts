import { describe, expect, it } from "vitest";
import { curatedOutHarnesses } from "./useCuratedOutHarnesses";

describe("curatedOutHarnesses", () => {
  it("leaves out a harness whose hidden built-in no pickable agent replaces", () => {
    const out = curatedOutHarnesses(new Set(["codex-native-ui", "pi-native-ui"]), [
      { harness: "claude-native" },
      // A custom agent on Pi still needs Pi set up.
      { harness: "pi-native" },
    ]);

    expect([...out]).toEqual(["codex-native"]);
  });

  it("matches a pickable agent through a harness alias", () => {
    expect(curatedOutHarnesses(new Set(["pi-native-ui"]), [{ harness: "native-pi" }])).toEqual(
      new Set(),
    );
  });

  it("leaves everything in while an agent's harness is unknown", () => {
    // The server lists an agent whose bundle it couldn't read without a harness.
    expect(
      curatedOutHarnesses(new Set(["codex-native-ui"]), [
        { harness: "claude-native" },
        { harness: null },
      ]),
    ).toEqual(new Set());
  });

  it("leaves everything in when the server hides nothing", () => {
    expect(curatedOutHarnesses(new Set(), [])).toEqual(new Set());
  });

  it("ignores hidden names that are not native built-ins", () => {
    expect(curatedOutHarnesses(new Set(["polly", "devin"]), [])).toEqual(new Set());
  });
});
