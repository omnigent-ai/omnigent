import { describe, expect, it } from "vitest";

import { getSafeAreaCollisionBoundary, withSafeAreaCollisionBoundary } from "./safeAreaInsets";

describe("getSafeAreaCollisionBoundary", () => {
  it("attaches one shared element inset by the safe-area variables", () => {
    const el = getSafeAreaCollisionBoundary();
    expect(el.isConnected).toBe(true);
    expect(getSafeAreaCollisionBoundary()).toBe(el);
    expect(el.style.position).toBe("fixed");
    expect(el.style.top).toBe("var(--omnigent-safe-top, 0px)");
    expect(el.style.bottom).toBe("var(--omnigent-safe-bottom, 0px)");
    expect(el.style.left).toBe("var(--omnigent-safe-left, 0px)");
    expect(el.style.right).toBe("var(--omnigent-safe-right, 0px)");
  });

  it("stays invisible and inert", () => {
    const el = getSafeAreaCollisionBoundary();
    expect(el.style.visibility).toBe("hidden");
    expect(el.style.pointerEvents).toBe("none");
    expect(el.getAttribute("aria-hidden")).toBe("true");
  });

  it("re-attaches after the element is detached", () => {
    const el = getSafeAreaCollisionBoundary();
    el.remove();
    expect(getSafeAreaCollisionBoundary().isConnected).toBe(true);
  });
});

describe("withSafeAreaCollisionBoundary", () => {
  it("yields just the safe-area boundary when the caller has none", () => {
    expect(withSafeAreaCollisionBoundary(undefined)).toEqual([getSafeAreaCollisionBoundary()]);
    expect(withSafeAreaCollisionBoundary(null)).toEqual([getSafeAreaCollisionBoundary()]);
  });

  it("keeps the caller's boundaries alongside the safe-area one", () => {
    const own = document.createElement("div");
    expect(withSafeAreaCollisionBoundary(own)).toEqual([own, getSafeAreaCollisionBoundary()]);
    expect(withSafeAreaCollisionBoundary([own, null])).toEqual([
      own,
      null,
      getSafeAreaCollisionBoundary(),
    ]);
  });
});
