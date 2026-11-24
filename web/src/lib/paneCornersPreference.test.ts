import { afterEach, describe, expect, it } from "vitest";

import {
  PANE_CORNERS_STORAGE_KEY,
  paneCornersPreference,
  resolveFloatingPanes,
} from "./paneCornersPreference";

afterEach(() => window.localStorage.clear());

describe("resolveFloatingPanes", () => {
  it.each([
    ["automatic", true, true, true],
    ["automatic", false, true, false],
    ["floating", false, true, true],
    ["sharp", true, true, false],
    ["floating", true, false, false],
    ["automatic", true, false, false],
  ] as const)(
    "%s with the policy %s and the design available %s floats: %s",
    (choice, policyFloating, designAvailable, floating) => {
      expect(resolveFloatingPanes({ choice, policyFloating, designAvailable })).toBe(floating);
    },
  );
});

describe("paneCornersPreference", () => {
  it("stores only a manual choice, and reads an unknown value as Automatic", () => {
    expect(paneCornersPreference.read()).toBe("automatic");
    paneCornersPreference.write("sharp");
    expect(window.localStorage.getItem(PANE_CORNERS_STORAGE_KEY)).toBe("sharp");
    paneCornersPreference.write("automatic");
    expect(window.localStorage.getItem(PANE_CORNERS_STORAGE_KEY)).toBeNull();
    window.localStorage.setItem(PANE_CORNERS_STORAGE_KEY, "rounded");
    expect(paneCornersPreference.read()).toBe("automatic");
  });
});
