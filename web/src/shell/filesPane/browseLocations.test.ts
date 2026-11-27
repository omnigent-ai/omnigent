import { describe, expect, it } from "vitest";

import {
  browseLocationChanges,
  rememberBrowseLocation,
  rememberedBrowseLocation,
} from "./browseLocations";

describe("browseLocationChanges", () => {
  it("counts each move of a session's folder, and not a choice of the folder it already shows", () => {
    expect(browseLocationChanges("count-session")).toBe(0);
    rememberBrowseLocation("count-session", "/repo/.worktrees/feature");
    rememberBrowseLocation("count-session", "/repo/.worktrees/feature");
    expect(browseLocationChanges("count-session")).toBe(1);
    rememberBrowseLocation("count-session", null);
    expect(rememberedBrowseLocation("count-session")).toBeNull();
    expect(browseLocationChanges("count-session")).toBe(2);
    expect(browseLocationChanges("other-session")).toBe(0);
  });
});
