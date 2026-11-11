import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  createEngineSwitch,
  DIFF_VIEWER_ENGINE,
  engineFor,
  FILE_VIEWER_ENGINE,
  PULL_REQUESTS_PAGE_ENGINE,
  readEngine,
  useEngine,
} from "./enginePreferences";

// Covers engine resolution; storedChoicePreference.test.ts covers shared storage.
const STORAGE_KEY = "test:sample-engine";

function sampleSwitch(offered: boolean) {
  return createEngineSwitch<"basic" | "fancy">({
    storageKey: STORAGE_KEY,
    engines: ["basic", "fancy"],
    automaticEngine: "basic",
    offered,
  });
}

afterEach(() => {
  localStorage.clear();
});

describe("an offered engine switch", () => {
  it("runs its Automatic engine until an engine is pinned", () => {
    const engineSwitch = sampleSwitch(true);
    expect(engineSwitch.intent.read()).toBe("automatic");
    expect(readEngine(engineSwitch)).toBe("basic");

    engineSwitch.intent.write("fancy");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("fancy");
    expect(readEngine(engineSwitch)).toBe("fancy");
  });

  it("stores nothing for Automatic, so the device follows a later Automatic engine", () => {
    const engineSwitch = sampleSwitch(true);
    engineSwitch.intent.write("fancy");
    engineSwitch.intent.write("automatic");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("treats a stored value naming no engine as Automatic", () => {
    localStorage.setItem(STORAGE_KEY, "retired");
    expect(sampleSwitch(true).intent.read()).toBe("automatic");
    expect(readEngine(sampleSwitch(true))).toBe("basic");
  });

  it("follows a pin made in another window", () => {
    const engineSwitch = sampleSwitch(true);
    const { result } = renderHook(() => useEngine(engineSwitch));
    expect(result.current).toBe("basic");

    act(() => {
      localStorage.setItem(STORAGE_KEY, "fancy");
      window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
    });
    expect(result.current).toBe("fancy");
  });
});

describe("a switch not offered yet", () => {
  it("runs its Automatic engine whatever is stored, and keeps the stored pin", () => {
    const engineSwitch = sampleSwitch(false);
    localStorage.setItem(STORAGE_KEY, "fancy");

    expect(engineFor(engineSwitch, "fancy")).toBe("basic");
    expect(readEngine(engineSwitch)).toBe("basic");
    expect(renderHook(() => useEngine(engineSwitch)).result.current).toBe("basic");
    expect(engineSwitch.intent.read()).toBe("fancy");
  });
});

describe.each([
  ["file viewer", FILE_VIEWER_ENGINE, "omnigent:file-viewer-engine"],
  ["diff viewer", DIFF_VIEWER_ENGINE, "omnigent:diff-viewer-engine"],
])("the %s switch", (_name, engineSwitch, storageKey) => {
  it("keeps its choice on this device under its own key", () => {
    expect(engineSwitch.intent.storageKey).toBe(storageKey);
  });

  it.each(["monaco", "codemirror", "pierre", ""])("runs Monaco with %j stored", (stored) => {
    localStorage.setItem(storageKey, stored);
    expect(readEngine(engineSwitch)).toBe("monaco");
  });
});

describe("the Pull Requests page switch", () => {
  it.each(["classic", "page", ""])("keeps the current page with %j stored", (stored) => {
    expect(PULL_REQUESTS_PAGE_ENGINE.intent.storageKey).toBe("omnigent:pull-requests-page-engine");
    localStorage.setItem(PULL_REQUESTS_PAGE_ENGINE.intent.storageKey, stored);
    expect(readEngine(PULL_REQUESTS_PAGE_ENGINE)).toBe("classic");
  });
});
