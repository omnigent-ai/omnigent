import { afterEach, describe, expect, it } from "vitest";

import {
  pendingTerminalFocus,
  requestTerminalFocus,
  settleTerminalFocus,
  TERMINAL_FOCUS_SURFACE_ATTRIBUTE,
  terminalFocusSurfaceId,
} from "./terminalFocusRequest";

afterEach(() => {
  settleTerminalFocus("conv", "main");
  settleTerminalFocus("other", "main");
  document.body.replaceChildren();
});

function element(tag: string, parent: HTMLElement = document.body): HTMLElement {
  const created = document.createElement(tag);
  parent.append(created);
  return created;
}

describe("terminal focus requests", () => {
  it("stay outstanding until taken, even before the surface exists", () => {
    requestTerminalFocus({ conversationId: "conv", surface: "main", tabKey: null });
    const request = pendingTerminalFocus("conv", "main");
    expect(request).toMatchObject({ conversationId: "conv", surface: "main", tabKey: null });
    expect(pendingTerminalFocus("conv", "rail")).toBeNull();

    // Settling an older request's id leaves a newer one in place.
    requestTerminalFocus({ conversationId: "conv", surface: "main", tabKey: "terminal:b" });
    const newer = pendingTerminalFocus("conv", "main")!;
    expect(newer.id).not.toBe(request!.id);
    settleTerminalFocus("conv", "main", request!.id);
    expect(pendingTerminalFocus("conv", "main")).toBe(newer);
    settleTerminalFocus("conv", "main", newer.id);
    expect(pendingTerminalFocus("conv", "main")).toBeNull();
  });

  it("lapse when focus moves outside the surface, but not within it", () => {
    const surface = element("div");
    surface.setAttribute(TERMINAL_FOCUS_SURFACE_ATTRIBUTE, terminalFocusSurfaceId("conv", "main"));
    const inside = element("button", surface);
    const elsewhere = element("button");

    requestTerminalFocus({ conversationId: "conv", surface: "main", tabKey: null });
    inside.focus();
    expect(pendingTerminalFocus("conv", "main")).not.toBeNull();
    elsewhere.focus();
    expect(pendingTerminalFocus("conv", "main")).toBeNull();
  });

  it("lapse when focus moves before the surface has mounted", () => {
    requestTerminalFocus({ conversationId: "other", surface: "main", tabKey: null });
    element("input").focus();
    expect(pendingTerminalFocus("other", "main")).toBeNull();
  });
});
