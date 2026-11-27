import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  type BrowserFlagDefinition,
  registerBrowserFlagDefinitions,
  resetBrowserFlagDefinitionsForTests,
} from "@/lib/browserFlagDefinitions";
import {
  BROWSER_FLAG_OVERRIDES_KEY,
  browserFlagOverride,
  browserFlagOverrides,
  browserFlags,
  resetBrowserFlagOverridesForTests,
  setBrowserFlagOverride,
  subscribeBrowserFlagOverrides,
} from "./browserFeatureFlagOverrides";

const FLAG = "example-page-flag";
const HIDDEN = "example-hidden-flag";
const SHELL_ONLY = "example-shell-flag";
const OFF_ONLY = "example-off-only-flag";
const DEFINITIONS: BrowserFlagDefinition[] = [
  { name: FLAG, type: "boolean", rendererVisible: true },
  { name: HIDDEN, type: "boolean", rendererVisible: false },
  { name: SHELL_ONLY, type: "boolean", rendererVisible: true, browser: false },
  { name: "example-number-flag", type: "number", rendererVisible: true },
  { name: OFF_ONLY, type: "boolean", rendererVisible: true, overrideValues: [false] },
];

const store = (value: unknown) =>
  window.localStorage.setItem(
    BROWSER_FLAG_OVERRIDES_KEY,
    typeof value === "string" ? value : JSON.stringify(value),
  );
const stored = () => JSON.parse(window.localStorage.getItem(BROWSER_FLAG_OVERRIDES_KEY) ?? "null");

beforeEach(() => {
  registerBrowserFlagDefinitions(DEFINITIONS);
});
afterEach(() => {
  window.localStorage.clear();
  resetBrowserFlagOverridesForTests();
  resetBrowserFlagDefinitionsForTests();
  vi.restoreAllMocks();
});

describe("browserFlags", () => {
  it("has the page-visible booleans a browser can act on, and not the shell-only ones", () => {
    expect(browserFlags().map((flag) => flag.name)).toEqual([FLAG, OFF_ONLY]);
  });

  it("has none until a build registers its definitions", () => {
    resetBrowserFlagDefinitionsForTests();
    expect(browserFlags()).toEqual([]);
  });
});

describe("the registered definitions", () => {
  it("override nothing without a registration, and read stored choices once registered", () => {
    resetBrowserFlagDefinitionsForTests();
    store({ [FLAG]: true });
    expect(browserFlagOverride(FLAG)).toBeUndefined();
    expect(setBrowserFlagOverride(FLAG, false)).toBe(false);
    registerBrowserFlagDefinitions(DEFINITIONS);
    expect(browserFlagOverride(FLAG)).toBe(true);
  });

  it("let a browser set only the values a flag allows", () => {
    expect(setBrowserFlagOverride(OFF_ONLY, true)).toBe(false);
    expect(setBrowserFlagOverride(OFF_ONLY, false)).toBe(true);
    store({ [OFF_ONLY]: true });
    expect(browserFlagOverrides()).toEqual({});
  });
});

describe("browserFlagOverrides", () => {
  it("reads none from empty, malformed or wrong-shaped storage", () => {
    expect(browserFlagOverrides()).toEqual({});
    for (const value of ["{not json", "[true]", "null", '"on"']) {
      store(value);
      expect(browserFlagOverrides(), value).toEqual({});
    }
  });

  it("drops unknown names, hidden flags and values that aren't booleans", () => {
    store({ [FLAG]: true, [HIDDEN]: true, "not-a-flag": true, "server-ui-canvas": "on" });
    expect(browserFlagOverrides()).toEqual({ [FLAG]: true });
    // A registered name with a wrong-typed value, on its own, so no name check can hide it.
    for (const value of ["on", 1, null, { on: true }]) {
      store({ [FLAG]: value });
      expect(browserFlagOverrides(), JSON.stringify(value)).toEqual({});
    }
  });

  it("reads only its own choices, even for a flag named like an object's own members", () => {
    registerBrowserFlagDefinitions([
      ...DEFINITIONS,
      { name: "constructor", type: "boolean", rendererVisible: true },
      { name: "toString", type: "boolean", rendererVisible: true },
    ]);
    expect(browserFlagOverride("constructor")).toBeUndefined();
    expect(browserFlagOverride("toString")).toBeUndefined();
    expect(browserFlagOverride("hasOwnProperty")).toBeUndefined();
    setBrowserFlagOverride("constructor", true);
    setBrowserFlagOverride("toString", false);
    expect(browserFlagOverride("constructor")).toBe(true);
    expect(browserFlagOverride("toString")).toBe(false);
  });

  it("gives the same object until the choices change", () => {
    store({ [FLAG]: true });
    const first = browserFlagOverrides();
    expect(browserFlagOverrides()).toBe(first);
    setBrowserFlagOverride(FLAG, false);
    expect(browserFlagOverrides()).not.toBe(first);
  });
});

describe("setBrowserFlagOverride", () => {
  it("saves On and Off, clears with undefined, and removes the key when nothing is left", () => {
    expect(setBrowserFlagOverride(FLAG, true)).toBe(true);
    expect(stored()).toEqual({ [FLAG]: true });
    setBrowserFlagOverride(FLAG, false);
    expect(browserFlagOverrides()).toEqual({ [FLAG]: false });
    setBrowserFlagOverride(FLAG, undefined);
    expect(window.localStorage.getItem(BROWSER_FLAG_OVERRIDES_KEY)).toBeNull();
  });

  it("keeps a stored choice for a flag this build hides, so showing it again restores it", () => {
    store({ [HIDDEN]: true });
    setBrowserFlagOverride(FLAG, true);
    expect(stored()).toEqual({ [HIDDEN]: true, [FLAG]: true });
  });

  it("refuses flags a browser can't set", () => {
    expect(setBrowserFlagOverride(HIDDEN, true)).toBe(false);
    expect(setBrowserFlagOverride("not-a-flag", true)).toBe(false);
    expect(window.localStorage.getItem(BROWSER_FLAG_OVERRIDES_KEY)).toBeNull();
  });

  it("says so when storage refuses the write", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    expect(setBrowserFlagOverride(FLAG, true)).toBe(false);
  });

  it.each([
    ["sets", true],
    ["clears", undefined],
  ])("%s nothing, and says so, when storage refuses the read", (_case, value) => {
    store({ [HIDDEN]: true, [FLAG]: false });
    const before = window.localStorage.getItem(BROWSER_FLAG_OVERRIDES_KEY);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    expect(setBrowserFlagOverride(FLAG, value)).toBe(false);
    vi.mocked(Storage.prototype.getItem).mockRestore();
    expect(window.localStorage.getItem(BROWSER_FLAG_OVERRIDES_KEY)).toBe(before);
  });

  it("names the choices in the console when they change", () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    setBrowserFlagOverride(FLAG, true);
    expect(info).toHaveBeenLastCalledWith("[featureFlags] browser overrides: example-page-flag=on");
    setBrowserFlagOverride(FLAG, undefined);
    expect(info).toHaveBeenLastCalledWith("[featureFlags] browser overrides: none");
  });
});

describe("subscribeBrowserFlagOverrides", () => {
  it("tells this tab about its own writes", () => {
    const listener = vi.fn();
    const leave = subscribeBrowserFlagOverrides(listener);
    setBrowserFlagOverride(FLAG, true);
    expect(listener).toHaveBeenCalledTimes(1);
    leave();
    setBrowserFlagOverride(FLAG, false);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("hears another tab's writes and a storage clear, and ignores other keys", () => {
    const listener = vi.fn();
    const leave = subscribeBrowserFlagOverrides(listener);
    window.dispatchEvent(new StorageEvent("storage", { key: "some-other-key" }));
    expect(listener).not.toHaveBeenCalled();

    store({ [FLAG]: true });
    window.dispatchEvent(new StorageEvent("storage", { key: BROWSER_FLAG_OVERRIDES_KEY }));
    expect(listener).toHaveBeenCalledTimes(1);
    expect(browserFlagOverrides()).toEqual({ [FLAG]: true });

    window.localStorage.clear();
    window.dispatchEvent(new StorageEvent("storage", { key: null }));
    expect(listener).toHaveBeenCalledTimes(2);
    expect(browserFlagOverrides()).toEqual({});
    leave();
  });

  it("tells its listeners when new definitions change which stored choices apply", () => {
    resetBrowserFlagDefinitionsForTests();
    store({ [FLAG]: true });
    const listener = vi.fn();
    const leave = subscribeBrowserFlagOverrides(listener);
    expect(browserFlagOverrides()).toEqual({});
    registerBrowserFlagDefinitions(DEFINITIONS);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(browserFlagOverrides()).toEqual({ [FLAG]: true });
    registerBrowserFlagDefinitions(DEFINITIONS);
    expect(listener).toHaveBeenCalledTimes(1);
    leave();
  });
});
