import { useEffect, useState } from "react";
import {
  peekDesktopFeatureFlag,
  readDesktopFeatureFlag,
  subscribeDesktopFeatureFlag,
  type DesktopFeatureFlagReading,
  type DesktopFeatureFlagState,
} from "@/lib/desktopFeatureFlags";
import type { DesktopFeatureFlagMode } from "@/lib/nativeBridge";

/**
 * A remotely configured desktop flag, live. Starts at the shell's answer where another reader
 * on the page already has it, else at `fallback` (unresolved in "value" mode), and follows the
 * shell's answers; browsers and older shells settle on `fallback`. The fallback should be a
 * primitive or null so it stays stable between renders.
 */
export function useDesktopFeatureFlag<T extends boolean | number | null>(
  name: string,
  fallback: T,
  mode: DesktopFeatureFlagMode = "value",
): DesktopFeatureFlagState<T> {
  const [state, setState] = useState<DesktopFeatureFlagState<T>>(
    () => peekDesktopFeatureFlag(name, { fallback }) ?? { value: fallback, resolved: false },
  );
  useEffect(() => {
    // A view mounted again, such as for the next PR, keeps the answer instead of flashing back.
    const known = peekDesktopFeatureFlag(name, { fallback });
    setState(known ?? { value: fallback, resolved: false });
    return subscribeDesktopFeatureFlag(name, { fallback, mode }, (next) => {
      if (known && !next.resolved) return;
      setState(next);
    });
  }, [name, fallback, mode]);
  return state;
}

/**
 * `useDesktopFeatureFlag`, with where the value came from, for a caller that must tell a
 * shell's real answer from a fallback. Shares the shell subscription with other readers.
 * While `enabled` is false it reads nothing and stays pending.
 */
export function useDesktopFeatureFlagReading<T extends boolean | number | null>(
  name: string,
  fallback: T,
  mode: DesktopFeatureFlagMode = "value",
  enabled = true,
): DesktopFeatureFlagReading<T> {
  const [reading, setReading] = useState<DesktopFeatureFlagReading<T>>({
    value: fallback,
    resolved: false,
    source: "pending",
  });
  useEffect(() => {
    setReading({ value: fallback, resolved: false, source: "pending" });
    if (!enabled) return;
    return readDesktopFeatureFlag(name, { fallback, mode }, setReading);
  }, [name, fallback, mode, enabled]);
  return reading;
}

const REMEMBERED_FLAG_PREFIX = "omnigent:desktop-flag-last:";

function rememberedFlag(name: string): boolean | null {
  try {
    const stored = window.localStorage.getItem(REMEMBERED_FLAG_PREFIX + name);
    return stored === "true" ? true : stored === "false" ? false : null;
  } catch {
    return null;
  }
}

/**
 * A boolean desktop flag that starts from this device's last answer rather than `fallback`, so a
 * flag that changes the layout doesn't paint the other layout until the shell answers.
 */
export function useRememberedDesktopFeatureFlag(name: string, fallback: boolean): boolean {
  const [remembered] = useState(() => rememberedFlag(name));
  const { value, resolved } = useDesktopFeatureFlag(name, fallback);
  useEffect(() => {
    if (!resolved) return;
    try {
      window.localStorage.setItem(REMEMBERED_FLAG_PREFIX + name, String(value));
    } catch {
      // Without storage the next window starts from the fallback.
    }
  }, [name, value, resolved]);
  return resolved ? value : (remembered ?? value);
}
