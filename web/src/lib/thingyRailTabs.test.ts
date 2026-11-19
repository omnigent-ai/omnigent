import { afterEach, describe, expect, it } from "vitest";

import {
  clearThingyRailRequest,
  exitThingyRailFullScreen,
  requestThingyRailTab,
  resetThingyRailRequests,
  setThingyNewSessionTabMaximized,
  useThingyRailRequest,
  withThingyFullScreen,
} from "./thingyRailTabs";

afterEach(resetThingyRailRequests);

describe("thingy rail requests", () => {
  it("keeps the latest request for a session's tab until it is cleared", () => {
    requestThingyRailTab("conv_1", "thingy:a");
    requestThingyRailTab("conv_2", "thingy:b", { maximize: true, keepSidebar: true });
    expect(useThingyRailRequest.getState().requested).toEqual({
      conversationId: "conv_2",
      key: "thingy:b",
      maximize: true,
      keepSidebar: true,
    });
    clearThingyRailRequest();
    expect(useThingyRailRequest.getState().requested).toBeNull();
  });

  it("takes a session's tab out of full screen, or the thingy beside New session", () => {
    setThingyNewSessionTabMaximized(true);
    exitThingyRailFullScreen("conv_1");
    expect(useThingyRailRequest.getState()).toMatchObject({
      exitFullScreen: { conversationId: "conv_1" },
      newSessionMaximized: true,
    });
    exitThingyRailFullScreen(null);
    expect(useThingyRailRequest.getState().newSessionMaximized).toBe(false);
  });

  it("names the thingy filling the screen in a path, keeping its query and hash", () => {
    expect(withThingyFullScreen("/c/conv_1", "a")).toBe("/c/conv_1?thingy=a");
    expect(withThingyFullScreen("/?prompt=x%3Fy&thingy_start=a#end", "a")).toBe(
      "/?prompt=x%3Fy&thingy_start=a&thingy=a#end",
    );
  });
});
