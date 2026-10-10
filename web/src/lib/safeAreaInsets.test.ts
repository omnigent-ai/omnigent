import { afterEach, describe, expect, it } from "vitest";

import { setEmbedRoot } from "@/lib/host";
import {
  getSafeAreaCollisionBoundary,
  mountSafeAreaCollisionBoundary,
  withSafeAreaCollisionBoundary,
} from "./safeAreaInsets";

afterEach(() => {
  setEmbedRoot(null);
});

describe("getSafeAreaCollisionBoundary", () => {
  it("creates one shared element inset by the safe-area variables", () => {
    const el = getSafeAreaCollisionBoundary();
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
});

describe("mountSafeAreaCollisionBoundary", () => {
  it("attaches the element to the document body and re-attaches after removal", () => {
    mountSafeAreaCollisionBoundary();
    const el = getSafeAreaCollisionBoundary();
    expect(el.parentElement).toBe(document.body);
    el.remove();
    mountSafeAreaCollisionBoundary();
    expect(el.parentElement).toBe(document.body);
  });

  it("follows an embed root registered after the first mount", () => {
    mountSafeAreaCollisionBoundary();
    const el = getSafeAreaCollisionBoundary();
    expect(el.parentElement).toBe(document.body);

    const root = document.createElement("div");
    document.body.appendChild(root);
    setEmbedRoot(root);
    mountSafeAreaCollisionBoundary();
    expect(getSafeAreaCollisionBoundary()).toBe(el);
    expect(el.parentElement).toBe(root);

    setEmbedRoot(null);
    mountSafeAreaCollisionBoundary();
    expect(el.parentElement).toBe(document.body);
    root.remove();
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
