import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearAppearancePreferenceRegistryForTests,
  createLocalPreference,
  getAppearanceStorageKeys,
  resetAppearancePreferences,
} from "./index";

afterEach(() => {
  localStorage.clear();
  clearAppearancePreferenceRegistryForTests();
});

describe("createLocalPreference", () => {
  it("returns the default when nothing is stored", () => {
    const pref = createLocalPreference({
      key: "test:scalar",
      defaultValue: 16,
      parse: (raw) => Number(raw),
      serialize: (value) => String(value),
    });
    expect(pref.read()).toBe(16);
    expect(localStorage.getItem("test:scalar")).toBeNull();
  });

  it("round-trips a value through localStorage", () => {
    const pref = createLocalPreference({
      key: "test:scalar",
      defaultValue: 16,
      parse: (raw) => Number(raw),
      serialize: (value) => String(value),
    });
    pref.write(18);
    expect(localStorage.getItem("test:scalar")).toBe("18");
    expect(pref.read()).toBe(18);
  });

  it("falls back to the default on corrupt stored values", () => {
    const pref = createLocalPreference({
      key: "test:json",
      defaultValue: 16,
      parse: (raw) => {
        try {
          const parsed: unknown = JSON.parse(raw);
          return typeof parsed === "number" && Number.isFinite(parsed) ? parsed : 16;
        } catch {
          return 16;
        }
      },
      serialize: (value) => JSON.stringify(value),
    });
    localStorage.setItem("test:json", "}{not json");
    expect(pref.read()).toBe(16);
  });

  it("normalizes on write and read", () => {
    const clamp = (n: number) => Math.min(20, Math.max(12, Math.round(n)));
    const pref = createLocalPreference({
      key: "test:clamped",
      defaultValue: 16,
      parse: (raw) => Number(raw),
      serialize: (value) => String(value),
      normalize: clamp,
    });
    pref.write(99);
    expect(pref.read()).toBe(20);
    expect(localStorage.getItem("test:clamped")).toBe("20");

    localStorage.setItem("test:clamped", "4");
    expect(pref.read()).toBe(12);
  });

  it("removes the key when writing the default if clearWhenDefault is set", () => {
    const pref = createLocalPreference({
      key: "test:enum",
      defaultValue: "auto" as const,
      parse: (raw) => (raw === "light" || raw === "dark" ? raw : "auto"),
      serialize: (value) => value,
      clearWhenDefault: true,
    });
    pref.write("dark");
    expect(localStorage.getItem("test:enum")).toBe("dark");
    pref.write("auto");
    expect(localStorage.getItem("test:enum")).toBeNull();
    expect(pref.read()).toBe("auto");
  });

  it("reset clears storage and notifies with the default", () => {
    const onChange = vi.fn();
    const pref = createLocalPreference({
      key: "test:reset",
      defaultValue: 16,
      parse: (raw) => Number(raw),
      serialize: (value) => String(value),
      onChange,
    });
    pref.write(18);
    onChange.mockClear();

    const storeCb = vi.fn();
    const valueCb = vi.fn();
    pref.subscribe(storeCb);
    pref.subscribeValue(valueCb);

    pref.reset();
    expect(localStorage.getItem("test:reset")).toBeNull();
    expect(pref.read()).toBe(16);
    expect(storeCb).toHaveBeenCalledOnce();
    expect(valueCb).toHaveBeenCalledWith(16);
    expect(onChange).toHaveBeenCalledWith(16);
  });

  it("notifies subscribers on write even when localStorage fails", () => {
    const pref = createLocalPreference({
      key: "test:quota",
      defaultValue: "a",
      parse: (raw) => raw,
      serialize: (value) => value,
    });
    const cb = vi.fn();
    pref.subscribeValue(cb);

    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    pref.write("b");
    expect(cb).toHaveBeenCalledWith("b");
    setItem.mockRestore();
  });

  it("stops notifying after unsubscribe", () => {
    const pref = createLocalPreference({
      key: "test:unsub",
      defaultValue: 1,
      parse: (raw) => Number(raw),
      serialize: (value) => String(value),
    });
    const cb = vi.fn();
    const unsub = pref.subscribeValue(cb);
    unsub();
    pref.write(2);
    expect(cb).not.toHaveBeenCalled();
  });

  it("registers with the appearance registry when appearance is true", () => {
    const pref = createLocalPreference({
      key: "test:appearance",
      defaultValue: "open" as const,
      parse: (raw) => (raw === "collapsed" ? "collapsed" : "open"),
      serialize: (value) => value,
      clearWhenDefault: true,
      appearance: true,
    });
    expect(getAppearanceStorageKeys()).toContain("test:appearance");

    pref.write("collapsed");
    expect(localStorage.getItem("test:appearance")).toBe("collapsed");
    resetAppearancePreferences();
    expect(localStorage.getItem("test:appearance")).toBeNull();
    expect(pref.read()).toBe("open");
  });
});

describe("createLocalPreference — migration / backward compatibility", () => {
  beforeEach(() => {
    clearAppearancePreferenceRegistryForTests();
  });

  it("preserves an existing JSON number font-size seed (old format)", () => {
    // Old uiFontPreferences wrote JSON.stringify(px); seed exactly that.
    localStorage.setItem("omnigent:ui-font-size", JSON.stringify(18));

    const pref = createLocalPreference({
      key: "omnigent:ui-font-size",
      defaultValue: 16,
      parse: (raw) => {
        try {
          const parsed: unknown = JSON.parse(raw);
          return typeof parsed === "number" && Number.isFinite(parsed) ? parsed : 16;
        } catch {
          return 16;
        }
      },
      serialize: (value) => JSON.stringify(value),
      appearance: true,
    });

    expect(pref.read()).toBe(18);
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("18");
  });

  it("preserves an existing raw-string workspace-panel seed (old format)", () => {
    localStorage.setItem("omnigent:default-workspace-panel", "collapsed");

    const pref = createLocalPreference({
      key: "omnigent:default-workspace-panel",
      defaultValue: "open" as const,
      parse: (raw) => (raw === "collapsed" ? "collapsed" : "open"),
      serialize: (value) => value,
      clearWhenDefault: true,
      appearance: true,
    });

    expect(pref.read()).toBe("collapsed");
    expect(localStorage.getItem("omnigent:default-workspace-panel")).toBe("collapsed");
  });

  it("preserves an existing raw-string terminal-theme seed (old format)", () => {
    localStorage.setItem("omnigent:terminal-theme", "dark");

    const pref = createLocalPreference({
      key: "omnigent:terminal-theme",
      defaultValue: "auto" as const,
      parse: (raw) => (raw === "light" || raw === "dark" ? raw : "auto"),
      serialize: (value) => value,
      clearWhenDefault: true,
      appearance: true,
    });

    expect(pref.read()).toBe("dark");
    expect(localStorage.getItem("omnigent:terminal-theme")).toBe("dark");
  });

  it("runs an explicit parse migration when the stored shape changes", () => {
    // Hypothetical old format: bare number string → new JSON object wrapper.
    localStorage.setItem("test:migrated", "19");

    const pref = createLocalPreference({
      key: "test:migrated",
      defaultValue: { size: 16 },
      parse: (raw) => {
        try {
          const parsed: unknown = JSON.parse(raw);
          if (typeof parsed === "number" && Number.isFinite(parsed)) {
            return { size: parsed };
          }
          if (
            typeof parsed === "object" &&
            parsed !== null &&
            "size" in parsed &&
            typeof (parsed as { size: unknown }).size === "number"
          ) {
            return { size: (parsed as { size: number }).size };
          }
        } catch {
          const asNumber = Number(raw);
          if (Number.isFinite(asNumber)) return { size: asNumber };
        }
        return { size: 16 };
      },
      serialize: (value) => JSON.stringify(value),
    });

    expect(pref.read()).toEqual({ size: 19 });
    pref.write({ size: 19 });
    expect(localStorage.getItem("test:migrated")).toBe(JSON.stringify({ size: 19 }));
  });
});
