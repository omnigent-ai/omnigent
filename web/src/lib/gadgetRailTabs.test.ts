import { afterEach, describe, expect, it } from "vitest";

import {
  clearGadgetRailRequest,
  exitGadgetRailFullScreen,
  gadgetCount,
  noteGadgetFrameConnected,
  noteGadgetStartFailed,
  requestGadgetRailTab,
  resetGadgetRailRequests,
  setGadgetNewSessionTabMaximized,
  useGadgetRailRequest,
  withGadgetFullScreen,
} from "./gadgetRailTabs";

afterEach(resetGadgetRailRequests);

describe("gadget rail requests", () => {
  it("keeps the latest request for a session's tab until it is cleared", () => {
    requestGadgetRailTab("conv_1", "gadget:a");
    requestGadgetRailTab("conv_2", "gadget:b", { maximize: true, keepSidebar: true });
    expect(useGadgetRailRequest.getState().requested).toEqual({
      conversationId: "conv_2",
      key: "gadget:b",
      maximize: true,
      keepSidebar: true,
    });
    clearGadgetRailRequest();
    expect(useGadgetRailRequest.getState().requested).toBeNull();
  });

  it("takes a session's tab out of full screen, or the gadget beside New session", () => {
    setGadgetNewSessionTabMaximized(true);
    exitGadgetRailFullScreen("conv_1");
    expect(useGadgetRailRequest.getState()).toMatchObject({
      exitFullScreen: { conversationId: "conv_1" },
      newSessionMaximized: true,
    });
    exitGadgetRailFullScreen(null);
    expect(useGadgetRailRequest.getState().newSessionMaximized).toBe(false);
  });

  it("names the gadget filling the screen in a path, keeping its query and hash", () => {
    expect(withGadgetFullScreen("/c/conv_1", "a")).toBe("/c/conv_1?gadget=a");
    expect(withGadgetFullScreen("/?prompt=x%3Fy&gadget_start=a#end", "a")).toBe(
      "/?prompt=x%3Fy&gadget_start=a&gadget=a#end",
    );
  });
});

describe("per-gadget counts", () => {
  it("counts a gadget whose ID is also an Object property like any other", () => {
    expect(gadgetCount({}, "constructor")).toBe(0);
    noteGadgetFrameConnected("constructor");
    noteGadgetFrameConnected("constructor");
    noteGadgetStartFailed("constructor");
    const state = useGadgetRailRequest.getState();
    expect(gadgetCount(state.frameConnections, "constructor")).toBe(2);
    expect(gadgetCount(state.failedStarts, "constructor")).toBe(1);
    expect(gadgetCount(state.frameConnections, "toString")).toBe(0);
  });
});
