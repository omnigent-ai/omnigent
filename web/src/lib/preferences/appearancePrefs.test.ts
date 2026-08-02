/**
 * Anti-rot / load-order guards for the Appearance preference registry.
 *
 * These tests fail CI when:
 * - A preference module isn't wired into the eager barrel (registry incomplete).
 * - EXPECTED_APPEARANCE_STORAGE_KEYS drifts from what's actually registered.
 * - A key is half-migrated (present in both registry and the legacy list).
 */

import { describe, expect, it } from "vitest";
import {
  EXPECTED_APPEARANCE_STORAGE_KEYS,
  LEGACY_APPEARANCE_STORAGE_KEYS,
} from "./appearancePrefs";
import { getAppearanceStorageKeys } from "./appearanceRegistry";

function sorted(keys: readonly string[]): string[] {
  return [...keys].sort();
}

describe("appearance preference registry — load order", () => {
  it("registers every expected key after the eager barrel loads", () => {
    // appearancePrefs.ts is imported above; its side-effect imports must
    // have registered every key in EXPECTED_APPEARANCE_STORAGE_KEYS. If a
    // new preference is added with appearance: true but not imported here,
    // this fails — Reset would silently skip it in production.
    expect(sorted(getAppearanceStorageKeys())).toEqual(sorted(EXPECTED_APPEARANCE_STORAGE_KEYS));
  });

  it("does not leave any key in both the registry and the legacy list", () => {
    // Half-migrated state: still cleared by LEGACY_APPEARANCE_STORAGE_KEYS
    // AND registered — the dual-list rot this layer exists to prevent.
    const registered = new Set(getAppearanceStorageKeys());
    const overlap = LEGACY_APPEARANCE_STORAGE_KEYS.filter((key) => registered.has(key));
    expect(overlap).toEqual([]);
  });

  it("keeps expected and legacy key lists disjoint by construction", () => {
    const expected = new Set<string>(EXPECTED_APPEARANCE_STORAGE_KEYS);
    const overlap = LEGACY_APPEARANCE_STORAGE_KEYS.filter((key) => expected.has(key));
    expect(overlap).toEqual([]);
  });
});
