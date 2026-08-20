import { afterEach, describe, expect, it, vi } from "vitest";

import { isSurfaceFrontmost } from "./useNativeServerSwitcher";

// The native Liquid Glass Chat/Terminal bar floats over the web view, so DOM
// stacking can't hide it — its visibility rides on `isSurfaceFrontmost`. A
// Radix menu drops `pointer-events: none` on <body>, making the centre probe
// fall through to the document root; that's normally a transient layer we keep
// the surface "frontmost" through. But the session kebab menu lives INSIDE the
// mobile sidebar overlay, so opening it must NOT re-float the bar over the
// sidebar. These tests pin that regression.

const VIEWPORT = 400;

function stubHitTest(topElement: Element | null): void {
  // jsdom doesn't implement elementFromPoint, so assign it outright.
  (document as unknown as { elementFromPoint: () => Element | null }).elementFromPoint = () =>
    topElement;
}

function rect(overrides: Partial<DOMRect>): DOMRect {
  return {
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    width: 0,
    height: 0,
    toJSON: () => ({}),
    ...overrides,
  } as DOMRect;
}

function makeSurface(): HTMLElement {
  const el = document.createElement("div");
  // Full-viewport chat surface centred under the probe point.
  el.getBoundingClientRect = () =>
    rect({ top: 0, left: 0, right: VIEWPORT, bottom: 800, width: VIEWPORT, height: 800 });
  document.body.appendChild(el);
  return el;
}

function makeOpenSidebar(): HTMLElement {
  const aside = document.createElement("aside");
  aside.className = "conversations-sidebar";
  // Open == no `data-collapsed`; full-screen overlay covering the probe.
  aside.getBoundingClientRect = () =>
    rect({ top: 0, left: 0, right: VIEWPORT, bottom: 800, width: VIEWPORT, height: 800 });
  document.body.appendChild(aside);
  return aside;
}

afterEach(() => {
  vi.unstubAllGlobals();
  delete (document as unknown as Record<string, unknown>).elementFromPoint;
  document.body.innerHTML = "";
});

describe("isSurfaceFrontmost", () => {
  it("returns false when a menu's body fall-through sits over an open sidebar", () => {
    vi.stubGlobal("innerWidth", VIEWPORT);
    const surface = makeSurface();
    makeOpenSidebar();
    // Radix set pointer-events:none on <body>, so the hit test falls through.
    stubHitTest(document.body);

    expect(isSurfaceFrontmost(surface)).toBe(false);
  });

  it("stays frontmost through a menu when the sidebar is collapsed", () => {
    vi.stubGlobal("innerWidth", VIEWPORT);
    const surface = makeSurface();
    const sidebar = makeOpenSidebar();
    sidebar.setAttribute("data-collapsed", "");
    stubHitTest(document.body);

    expect(isSurfaceFrontmost(surface)).toBe(true);
  });

  it("stays frontmost through a menu when no sidebar is present", () => {
    vi.stubGlobal("innerWidth", VIEWPORT);
    const surface = makeSurface();
    stubHitTest(document.body);

    expect(isSurfaceFrontmost(surface)).toBe(true);
  });

  it("returns false when the menu popper covers the probe over an open sidebar", () => {
    vi.stubGlobal("innerWidth", VIEWPORT);
    const surface = makeSurface();
    makeOpenSidebar();
    const menu = document.createElement("div");
    menu.setAttribute("role", "menu");
    document.body.appendChild(menu);
    stubHitTest(menu);

    expect(isSurfaceFrontmost(surface)).toBe(false);
  });
});
