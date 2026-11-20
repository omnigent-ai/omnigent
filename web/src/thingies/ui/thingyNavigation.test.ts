import { describe, expect, it } from "vitest";

import { thingyOpenKeepsSidebar } from "./thingyNavigation";

describe("whether opening a thingy full screen leaves the sidebar as it is", () => {
  it("keeps it for an open from inside Omnigent", () => {
    expect(thingyOpenKeepsSidebar({ key: "k1", state: null })).toBe(true);
  });

  it("closes it for the page the browser loaded from a link", () => {
    expect(thingyOpenKeepsSidebar({ key: "default", state: null })).toBe(false);
  });

  it("follows link state that says so", () => {
    expect(thingyOpenKeepsSidebar({ key: "k1", state: { keepSidebar: false } })).toBe(false);
    expect(thingyOpenKeepsSidebar({ key: "default", state: { keepSidebar: true } })).toBe(true);
  });
});
