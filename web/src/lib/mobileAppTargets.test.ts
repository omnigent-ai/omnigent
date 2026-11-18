import { afterEach, describe, expect, it, vi } from "vitest";
import { getStyleRoot, setEmbedScopeRoot } from "./host";
import { applyMobileAppTargetAttribute } from "./mobileAppTargets";

afterEach(() => {
  setEmbedScopeRoot(null);
  delete document.documentElement.dataset.mobileApp;
  vi.unstubAllGlobals();
});

describe("mobile app target eligibility", () => {
  it.each(["ios", "android", "electron", "browser"])(
    "marks only mobile shells in %s at either style root",
    (kind) => {
      vi.stubGlobal("omnigentNative", kind === "ios" || kind === "android" ? { kind } : undefined);
      vi.stubGlobal("omnigentDesktop", kind === "electron" ? { kind } : undefined);
      const expected = kind === "ios" || kind === "android" ? "true" : undefined;
      applyMobileAppTargetAttribute();
      expect(document.documentElement.dataset.mobileApp).toBe(expected);
      delete document.documentElement.dataset.mobileApp;

      const scope = document.createElement("div");
      setEmbedScopeRoot(scope);
      applyMobileAppTargetAttribute();
      expect(getStyleRoot()).toBe(scope);
      expect(scope.dataset.mobileApp).toBe(expected);
      expect(document.documentElement.dataset.mobileApp).toBeUndefined();
    },
  );

  it("clears a stale marker when an embed root is reused in a browser", () => {
    const scope = document.createElement("div");
    scope.dataset.mobileApp = "true";
    setEmbedScopeRoot(scope);
    applyMobileAppTargetAttribute();
    expect(scope.dataset.mobileApp).toBeUndefined();
  });
});
