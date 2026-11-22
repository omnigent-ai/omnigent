import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { type StripEntry, useTabStripPresence } from "./useTabStripPresence";

const entries = (...keys: string[]): StripEntry[] =>
  keys.map((key) => ({ key, tab: <span>{key}</span> }));
const phases = (result: ReturnType<typeof useTabStripPresence>) =>
  result.map((entry) => `${entry.key}:${entry.motion.phase}`);

describe("useTabStripPresence", () => {
  it("animates tabs that come and go within one scope", () => {
    const { result, rerender } = renderHook(({ scope, tabs }) => useTabStripPresence(scope, tabs), {
      initialProps: { scope: "session-a", tabs: entries("a1", "a2") },
    });
    expect(phases(result.current)).toEqual(["a1:present", "a2:present"]);
    rerender({ scope: "session-a", tabs: entries("a1", "a2", "a3") });
    expect(phases(result.current)).toEqual(["a1:present", "a2:present", "a3:entering"]);
  });

  it("shows another scope's tabs as they are, leaving no ghosts of the last scope's", () => {
    const { result, rerender } = renderHook(({ scope, tabs }) => useTabStripPresence(scope, tabs), {
      initialProps: { scope: "session-a", tabs: entries("a1", "a2", "a3") },
    });
    rerender({ scope: "session-b", tabs: entries("b1") });
    expect(phases(result.current)).toEqual(["b1:present"]);
    rerender({ scope: "session-a", tabs: entries("a1", "a2", "a3") });
    expect(phases(result.current)).toEqual(["a1:present", "a2:present", "a3:present"]);
  });

  it("keeps a closed tab's last picture for its ghost", () => {
    const { result, rerender } = renderHook(({ tabs }) => useTabStripPresence("session-a", tabs), {
      initialProps: { tabs: entries("a1", "a2") },
    });
    rerender({ tabs: entries("a1") });
    const ghost = result.current.find((entry) => entry.key === "a2");
    expect(ghost?.motion.phase).toBe("leaving");
    expect(ghost?.tab).toEqual(<span>a2</span>);
  });
});
