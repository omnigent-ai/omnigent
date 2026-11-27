import { afterEach, describe, expect, it } from "vitest";

import { isPageHiding } from "./pageLifecycle";

afterEach(() => {
  window.dispatchEvent(new Event("pageshow"));
});

describe("isPageHiding", () => {
  it("is true from pagehide until the page shows again", () => {
    expect(isPageHiding()).toBe(false);
    window.dispatchEvent(new Event("pagehide"));
    expect(isPageHiding()).toBe(true);
    window.dispatchEvent(new Event("pageshow"));
    expect(isPageHiding()).toBe(false);
  });
});
