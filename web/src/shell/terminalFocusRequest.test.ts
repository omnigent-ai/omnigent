import { afterEach, describe, expect, it, vi } from "vitest";

import {
  focusShownTerminalSurface,
  isTerminalSurfaceFocused,
  pendingTerminalFocus,
  releaseTerminalSurfaceFocus,
  requestTerminalFocus,
  settleTerminalFocus,
  TERMINAL_FOCUS_SURFACE_ATTRIBUTE,
  terminalFocusSurfaceId,
} from "./terminalFocusRequest";

afterEach(() => {
  vi.restoreAllMocks();
  settleTerminalFocus("conv", "main");
  settleTerminalFocus("conv", "rail");
  settleTerminalFocus("other", "main");
  document.body.replaceChildren();
});

function element(tag: string, parent: HTMLElement = document.body): HTMLElement {
  const created = document.createElement(tag);
  parent.append(created);
  return created;
}

function surfaceElement(
  conversationId: string,
  surface: "main" | "rail",
  parent: HTMLElement = document.body,
): HTMLElement {
  const created = element("div", parent);
  created.setAttribute(
    TERMINAL_FOCUS_SURFACE_ATTRIBUTE,
    terminalFocusSurfaceId(conversationId, surface),
  );
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

describe("terminal focus surface wrappers", () => {
  it("request focus only for a surface that is on screen", () => {
    surfaceElement("conv", "main");
    expect(focusShownTerminalSurface("conv", "main")).toBe(true);
    expect(pendingTerminalFocus("conv", "main")).toMatchObject({ tabKey: null });
    // No element for the rail surface, so nothing shows it.
    expect(focusShownTerminalSurface("conv", "rail")).toBe(false);
    expect(pendingTerminalFocus("conv", "rail")).toBeNull();
  });

  it.each([
    ["hidden", (surface: HTMLElement) => surface.setAttribute("hidden", "")],
    ["aria-hidden", (surface: HTMLElement) => surface.setAttribute("aria-hidden", "true")],
    ["inert", (surface: HTMLElement) => surface.setAttribute("inert", "")],
    [
      "inside an inert ancestor",
      (surface: HTMLElement) => surface.parentElement!.setAttribute("inert", ""),
    ],
    ["disconnected", (surface: HTMLElement) => surface.remove()],
  ])("don't request focus for a surface that is %s", (_state, hide) => {
    const surface = surfaceElement("conv", "main", element("section"));
    hide(surface);
    expect(focusShownTerminalSurface("conv", "main")).toBe(false);
    expect(pendingTerminalFocus("conv", "main")).toBeNull();
  });

  it("request focus when any one of the surface's elements shows", () => {
    surfaceElement("conv", "main").setAttribute("hidden", "");
    surfaceElement("conv", "main");
    expect(focusShownTerminalSurface("conv", "main")).toBe(true);
  });

  it("report focus only while the keyboard is inside that surface", () => {
    const main = element("button", surfaceElement("conv", "main"));
    const rail = element("button", surfaceElement("conv", "rail"));
    const elsewhere = element("button");

    main.focus();
    expect(isTerminalSurfaceFocused("conv", "main")).toBe(true);
    expect(isTerminalSurfaceFocused("conv", "rail")).toBe(false);
    expect(isTerminalSurfaceFocused("other", "main")).toBe(false);
    rail.focus();
    expect(isTerminalSurfaceFocused("conv", "main")).toBe(false);
    expect(isTerminalSurfaceFocused("conv", "rail")).toBe(true);
    elsewhere.focus();
    expect(isTerminalSurfaceFocused("conv", "rail")).toBe(false);
  });

  it("report no focus while the window itself lacks it", () => {
    element("button", surfaceElement("conv", "main")).focus();
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    expect(isTerminalSurfaceFocused("conv", "main")).toBe(false);
  });

  it("release blurs only the released surface's terminal", () => {
    const main = element("button", surfaceElement("conv", "main"));
    surfaceElement("conv", "rail");
    surfaceElement("other", "main");

    main.focus();
    releaseTerminalSurfaceFocus("conv", "rail");
    releaseTerminalSurfaceFocus("other", "main");
    expect(document.activeElement).toBe(main);
    releaseTerminalSurfaceFocus("conv", "main");
    expect(document.activeElement).toBe(document.body);
  });

  it("release leaves focus outside every surface alone", () => {
    surfaceElement("conv", "main");
    const elsewhere = element("input");
    elsewhere.focus();
    releaseTerminalSurfaceFocus("conv", "main");
    expect(document.activeElement).toBe(elsewhere);
  });
});
