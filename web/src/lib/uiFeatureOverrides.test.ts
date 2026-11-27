import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SERVER_INFO_OFFLINE_FALLBACK } from "./bootCapabilities";
import { isFeatureEnabled } from "./capabilities";
import { getOmnigentServerIdentity } from "./host";
import {
  activeUiFeatureOverrides,
  canBootstrapQuickPick,
  canOverrideUiFeatures,
  clearUiFeatureOverrides,
  readUiFeatureOverrides,
  resetUiFeatureOverridesForTests,
  writeUiFeatureOverride,
} from "./uiFeatureOverrides";

vi.mock("./host", () => ({ getOmnigentServerIdentity: vi.fn(), hostFetch: vi.fn() }));

const key = 'omnigent:ui-feature-overrides:v1:"server-a"';
const info = (features: Record<string, boolean>) => ({ ...SERVER_INFO_OFFLINE_FALLBACK, features });

beforeEach(() => {
  localStorage.clear();
  resetUiFeatureOverridesForTests();
  vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-a");
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  resetUiFeatureOverridesForTests();
});

describe("UI feature overrides", () => {
  it("scopes, writes, and removes decisions for each server", () => {
    expect(writeUiFeatureOverride("canvas", true)).toBe(true);
    expect(localStorage.getItem(key)).toBe('{"canvas":true}');
    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-b");
    expect(readUiFeatureOverrides()).toEqual({});
    expect(writeUiFeatureOverride("canvas", false)).toBe(true);
    expect(localStorage.getItem('omnigent:ui-feature-overrides:v1:"server-b"')).toBe(
      '{"canvas":false}',
    );
    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-a");
    expect(readUiFeatureOverrides()).toEqual({ canvas: true });
    expect(writeUiFeatureOverride("session_overview", false)).toBe(true);
    expect(writeUiFeatureOverride("canvas", null)).toBe(true);
    expect(readUiFeatureOverrides()).toEqual({ session_overview: false });
    expect(writeUiFeatureOverride("session_overview", null)).toBe(true);
    expect(localStorage.getItem(key)).toBeNull();
  });

  it("clears this server's entire entry and cached choices", () => {
    const otherKey = 'omnigent:ui-feature-overrides:v1:"server-b"';
    localStorage.setItem(
      key,
      JSON.stringify({ quick_pick_palette: true, canvas: false, unknown: true }),
    );
    localStorage.setItem(otherKey, '{"canvas":true}');
    expect(readUiFeatureOverrides()).toEqual({ quick_pick_palette: true, canvas: false });
    expect(clearUiFeatureOverrides()).toBe(true);
    expect(localStorage.getItem(key)).toBeNull();
    expect(localStorage.getItem(otherKey)).toBe('{"canvas":true}');
    expect(readUiFeatureOverrides()).toEqual({});
    localStorage.setItem(key, '{"canvas":true}');
    expect(readUiFeatureOverrides()).toEqual({});
  });

  it.each(["{", "null", "[]", '"true"'])("ignores malformed or non-object storage %s", (raw) => {
    localStorage.setItem(key, raw);
    expect(readUiFeatureOverrides()).toEqual({});
  });

  it("ignores unknown names and non-booleans and caches the read", () => {
    localStorage.setItem(
      key,
      JSON.stringify({ canvas: false, session_overview: "true", usage_page: true, unknown: true }),
    );
    expect(readUiFeatureOverrides()).toEqual({ canvas: false });
    localStorage.setItem(key, '{"canvas":true}');
    expect(readUiFeatureOverrides()).toEqual({ canvas: false });
  });

  it.each([
    { choice: false, expected: { canvas: false, session_overview: false } },
    { choice: null, expected: { session_overview: false } },
  ])("preserves another tab's changes when saving $choice", ({ choice, expected }) => {
    localStorage.setItem(key, '{"canvas":true}');
    expect(readUiFeatureOverrides()).toEqual({ canvas: true });
    localStorage.setItem(
      key,
      JSON.stringify({
        canvas: true,
        session_overview: false,
        usage_page: true,
        sidebar_grouping_ui: "invalid",
      }),
    );
    expect(readUiFeatureOverrides()).toEqual({ canvas: true });
    expect(writeUiFeatureOverride("canvas", choice)).toBe(true);
    expect(JSON.parse(localStorage.getItem(key)!)).toEqual(expected);
  });

  it("does not overwrite saved choices when a fresh read fails", () => {
    localStorage.setItem(key, '{"canvas":true,"session_overview":false}');
    expect(readUiFeatureOverrides()).toEqual({ canvas: true, session_overview: false });
    const read = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(writeUiFeatureOverride("canvas", null)).toBe(false);
    read.mockRestore();
    expect(localStorage.getItem(key)).toBe('{"canvas":true,"session_overview":false}');
  });

  it("ignores overrides for removed sidebar flags", () => {
    localStorage.setItem(
      key,
      JSON.stringify({
        sidebar_saved_views: true,
        sidebar_windowed_rows: false,
        workspace_sidebar_v2: true,
      }),
    );
    expect(readUiFeatureOverrides()).toEqual({});
    const reported = info({
      quick_pick_palette: true,
      sidebar_saved_views: false,
      sidebar_windowed_rows: true,
      workspace_sidebar_v2: false,
    });
    expect(isFeatureEnabled(reported, "workspace_sidebar_v2")).toBe(false);
    expect(isFeatureEnabled(reported, "sidebar_saved_views")).toBe(false);
    expect(isFeatureEnabled(reported, "sidebar_windowed_rows")).toBe(true);
    expect(activeUiFeatureOverrides(reported)).toEqual([]);
    expect(writeUiFeatureOverride("workspace_sidebar_v2", true)).toBe(false);
    expect(writeUiFeatureOverride("sidebar_saved_views", true)).toBe(false);
    expect(writeUiFeatureOverride("sidebar_windowed_rows", false)).toBe(false);
  });

  it("does not read or write without a server identity", () => {
    localStorage.setItem(key, '{"canvas":true}');
    vi.mocked(getOmnigentServerIdentity).mockReturnValue(null);
    expect(canOverrideUiFeatures()).toBe(false);
    expect(readUiFeatureOverrides()).toEqual({});
    expect(writeUiFeatureOverride("canvas", false)).toBe(false);
    expect(clearUiFeatureOverrides()).toBe(false);
    expect(localStorage.getItem(key)).toBe('{"canvas":true}');
  });

  it("handles storage failures and preserves the prior choice on failed writes", () => {
    const read = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(readUiFeatureOverrides()).toEqual({});
    expect(writeUiFeatureOverride("canvas", true)).toBe(false);
    read.mockRestore();
    expect(writeUiFeatureOverride("canvas", true)).toBe(true);
    const saved = localStorage.getItem(key);
    expect(readUiFeatureOverrides()).toEqual({ canvas: true });
    const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(writeUiFeatureOverride("canvas", false)).toBe(false);
    write.mockRestore();
    expect(localStorage.getItem(key)).toBe(saved);
    expect(readUiFeatureOverrides()).toEqual({ canvas: true });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(writeUiFeatureOverride("canvas", null)).toBe(false);
    expect(clearUiFeatureOverrides()).toBe(false);
    expect(localStorage.getItem(key)).toBe(saved);
    expect(readUiFeatureOverrides()).toEqual({ canvas: true });
  });

  it("offers bootstrap only when QuickPick is reported, effectively off, and scoped", () => {
    const off = info({ quick_pick_palette: false });
    const on = info({ quick_pick_palette: true });
    expect(canBootstrapQuickPick("loading")).toBe(false);
    expect(canBootstrapQuickPick(info({}))).toBe(false);
    expect(canBootstrapQuickPick(off)).toBe(true);
    expect(canBootstrapQuickPick(on)).toBe(false);
    expect(writeUiFeatureOverride("quick_pick_palette", true)).toBe(true);
    expect(canBootstrapQuickPick(off)).toBe(false);
    expect(writeUiFeatureOverride("quick_pick_palette", false)).toBe(true);
    expect(canBootstrapQuickPick(on)).toBe(true);
    vi.mocked(getOmnigentServerIdentity).mockReturnValue(null);
    expect(canBootstrapQuickPick(off)).toBe(false);
  });

  it("overrides warm switching on and off when the server advertises it as false", () => {
    const reported = info({ quick_pick_palette: false, warm_switch_keep_mounted_ui: false });
    expect(writeUiFeatureOverride("warm_switch_keep_mounted_ui", true)).toBe(true);
    expect(isFeatureEnabled(reported, "warm_switch_keep_mounted_ui")).toBe(false);
    expect(writeUiFeatureOverride("quick_pick_palette", true)).toBe(true);
    expect(isFeatureEnabled(reported, "warm_switch_keep_mounted_ui")).toBe(true);
    expect(writeUiFeatureOverride("warm_switch_keep_mounted_ui", false)).toBe(true);
    expect(isFeatureEnabled(reported, "warm_switch_keep_mounted_ui")).toBe(false);
  });

  it("gates other overrides on the effective reported QuickPick value", () => {
    localStorage.setItem(
      key,
      JSON.stringify({ quick_pick_palette: true, canvas: true, session_overview: false }),
    );
    const features = { quick_pick_palette: false, canvas: false, session_overview: true };
    expect(isFeatureEnabled("loading", "quick_pick_palette")).toBe(false);
    expect(isFeatureEnabled(info(features), "quick_pick_palette")).toBe(true);
    expect(isFeatureEnabled(info(features), "canvas")).toBe(true);
    expect(isFeatureEnabled(info(features), "session_overview")).toBe(false);
    expect(
      activeUiFeatureOverrides(info(features)).map(({ feature, value }) => [feature, value]),
    ).toEqual([
      ["quick_pick_palette", true],
      ["canvas", true],
      ["session_overview", false],
    ]);

    expect(writeUiFeatureOverride("quick_pick_palette", false)).toBe(true);
    expect(
      isFeatureEnabled(info({ ...features, quick_pick_palette: true }), "quick_pick_palette"),
    ).toBe(false);
    expect(isFeatureEnabled(info({ ...features, quick_pick_palette: true }), "canvas")).toBe(false);
    expect(
      isFeatureEnabled(info({ ...features, quick_pick_palette: true }), "session_overview"),
    ).toBe(true);
    expect(activeUiFeatureOverrides(info({ ...features, quick_pick_palette: true }))).toEqual([]);

    expect(writeUiFeatureOverride("quick_pick_palette", true)).toBe(true);
    expect(isFeatureEnabled(info({ canvas: false }), "quick_pick_palette")).toBe(false);
    expect(isFeatureEnabled(info({ canvas: false }), "canvas")).toBe(false);
    expect(activeUiFeatureOverrides(info({ canvas: false }))).toEqual([]);
    expect(writeUiFeatureOverride("quick_pick_palette", null)).toBe(true);
    expect(isFeatureEnabled(info(features), "quick_pick_palette")).toBe(false);
    expect(
      isFeatureEnabled(info({ ...features, quick_pick_palette: true }), "quick_pick_palette"),
    ).toBe(true);
  });

  it("applies only listed and reported flags with the palette enabled", () => {
    localStorage.setItem(
      key,
      JSON.stringify({
        canvas: true,
        session_overview: false,
        usage_page: true,
      }),
    );
    const features = {
      quick_pick_palette: true,
      canvas: false,
      session_overview: true,
      usage_page: false,
    };
    expect(isFeatureEnabled("loading", "canvas")).toBe(false);
    expect(isFeatureEnabled(info(features), "canvas")).toBe(true);
    expect(isFeatureEnabled(info(features), "session_overview")).toBe(false);
    expect(isFeatureEnabled(info(features), "usage_page")).toBe(false);
    expect(isFeatureEnabled(info(features), "quick_pick_palette")).toBe(true);
    expect(isFeatureEnabled(info({ quick_pick_palette: true }), "canvas")).toBe(false);
    expect(isFeatureEnabled(info({ ...features, quick_pick_palette: false }), "canvas")).toBe(
      false,
    );
    expect(
      activeUiFeatureOverrides(info(features)).map(({ feature, value }) => [feature, value]),
    ).toEqual([
      ["canvas", true],
      ["session_overview", false],
    ]);
    expect(activeUiFeatureOverrides(info({ ...features, quick_pick_palette: false }))).toEqual([]);
    expect(activeUiFeatureOverrides(info({ quick_pick_palette: true }))).toEqual([]);
    expect(writeUiFeatureOverride("canvas", null)).toBe(true);
    expect(isFeatureEnabled(info(features), "canvas")).toBe(false);
    expect(isFeatureEnabled(info({ ...features, canvas: true }), "canvas")).toBe(true);
  });
});
