// Remotely configured flags from the desktop shell. A shell without the bridge, or one that
// doesn't list the flag, answers with the caller's fallback, as resolved: an older shell simply
// has the flag off. A browser with no native shell answers with its own override, else the same.

import {
  browserFlagOverride,
  subscribeBrowserFlagOverrides,
} from "@/lib/browserFeatureFlagOverrides";
import {
  featureFlagBridge,
  getDesktopFeatures,
  isNativeShell,
  type DesktopFeatureFlagBridge,
  type DesktopFeatureFlagMode,
} from "@/lib/nativeBridge";

/** A flag's value as a page sees it; `resolved` is false only for a provisional default. */
export interface DesktopFeatureFlagState<T> {
  value: T;
  resolved: boolean;
}

export interface DesktopFeatureFlagOptions<T> {
  /** Used until the shell answers, and for good when it can't. */
  fallback: T;
  /** "value" shows the fallback at once; "resolved" waits for the shell's answer. */
  mode?: DesktopFeatureFlagMode;
  /** Whether a delivered value has the expected shape; defaults to "same type as fallback". */
  accepts?: (value: unknown) => value is T;
}

function sameTypeAs<T>(fallback: T): (value: unknown) => value is T {
  return (value: unknown): value is T => fallback === null || typeof value === typeof fallback;
}

/** One shell subscription to a flag, which every page consumer of that flag listens to. */
interface SharedFlagSubscription {
  listeners: Set<(value: unknown) => void>;
  /** The shell's last value, for a consumer that joins after it arrived. */
  latest: { value: unknown } | null;
  stop: () => void;
}

// The shell allows a window only a few live subscriptions, so consumers of one flag share one.
const sharedSubscriptions = new WeakMap<
  DesktopFeatureFlagBridge,
  Map<string, SharedFlagSubscription>
>();

/** Hand `value` to one consumer; a consumer that throws can't keep the others from hearing it. */
function deliver(listener: (value: unknown) => void, value: unknown): void {
  try {
    listener(value);
  } catch (error) {
    console.error("[desktopFeatureFlags] a flag consumer threw:", error);
  }
}

/** Listen to `name` through the bridge's shared subscription; throws if the bridge does. */
function joinSharedSubscription(
  bridge: DesktopFeatureFlagBridge,
  name: string,
  listener: (value: unknown) => void,
): () => void {
  let byName = sharedSubscriptions.get(bridge);
  if (!byName) {
    byName = new Map();
    sharedSubscriptions.set(bridge, byName);
  }
  let shared = byName.get(name);
  if (!shared) {
    const created: SharedFlagSubscription = { listeners: new Set(), latest: null, stop: () => {} };
    created.stop = bridge.subscribe(name, { mode: "resolved" }, (value) => {
      created.latest = { value };
      for (const each of [...created.listeners]) deliver(each, value);
    });
    shared = created;
    byName.set(name, shared);
  }
  const joined = shared;
  joined.listeners.add(listener);
  if (joined.latest) deliver(listener, joined.latest.value);
  const owners = byName;
  return () => {
    if (!joined.listeners.delete(listener) || joined.listeners.size > 0) return;
    if (owners.get(name) === joined) owners.delete(name);
    joined.stop();
  };
}

/**
 * Where a flag's value came from: the shell's answer, this browser's override, or why the page
 * fell back without one. "pending" is the provisional fallback before the shell has answered.
 */
export type DesktopFeatureFlagSource =
  | "pending"
  | "shell"
  | "browser_override"
  | "no_bridge"
  | "features_unavailable"
  | "not_listed"
  | "subscribe_threw";

/** A flag's state together with where it came from, so a fallback never reads as a real Off. */
export interface DesktopFeatureFlagReading<T> extends DesktopFeatureFlagState<T> {
  source: DesktopFeatureFlagSource;
}

/** This browser's override of `name`, when it has one of the expected shape. */
function browserOverrideOf<T>(name: string, accepts: (value: unknown) => value is T) {
  const value = browserFlagOverride(name);
  return value !== undefined && accepts(value) ? { value } : null;
}

/** A browser's reading: its override where it has one, else the fallback, followed live. */
function readBrowserFeatureFlag<T>(
  name: string,
  fallback: T,
  accepts: (value: unknown) => value is T,
  onChange: (reading: DesktopFeatureFlagReading<T>) => void,
): () => void {
  let active = true;
  let delivered: { value: T } | null | undefined;
  const deliverCurrent = () => {
    const override = browserOverrideOf(name, accepts);
    if (!active || (delivered !== undefined && delivered?.value === override?.value)) return;
    delivered = override;
    const reading: DesktopFeatureFlagReading<T> = override
      ? { value: override.value, resolved: true, source: "browser_override" }
      : { value: fallback, resolved: true, source: "no_bridge" };
    deliver(() => onChange(reading), undefined);
  };
  deliverCurrent();
  const leave = subscribeBrowserFlagOverrides(deliverCurrent);
  return () => {
    active = false;
    leave();
  };
}

/**
 * Subscribe to one desktop flag, hearing where each value came from. `onChange` hears the
 * provisional fallback at once in "value" mode, then each resolved value. Returns an
 * unsubscribe function. Consumers of one flag share one shell subscription.
 */
export function readDesktopFeatureFlag<T>(
  name: string,
  { fallback, mode = "value", accepts = sameTypeAs(fallback) }: DesktopFeatureFlagOptions<T>,
  onChange: (reading: DesktopFeatureFlagReading<T>) => void,
): () => void {
  let active = true;
  let unsubscribe: (() => void) | null = null;
  // Settles from a promise too, so a consumer that throws is logged, never an unhandled rejection.
  const settleOnFallback = (source: Exclude<DesktopFeatureFlagSource, "pending" | "shell">) => {
    if (active) deliver(() => onChange({ value: fallback, resolved: true, source }), undefined);
  };

  const bridge = featureFlagBridge();
  if (!bridge && !isNativeShell()) return readBrowserFeatureFlag(name, fallback, accepts, onChange);
  if (!bridge) {
    settleOnFallback("no_bridge");
    return () => {
      active = false;
    };
  }
  if (mode === "value") {
    deliver(() => onChange({ value: fallback, resolved: false, source: "pending" }), undefined);
  }

  void getDesktopFeatures().then((features) => {
    if (!active) return;
    if (!features) {
      settleOnFallback("features_unavailable");
      return;
    }
    const names = features.featureFlags?.names;
    if (!Array.isArray(names) || !names.includes(name)) {
      settleOnFallback("not_listed");
      return;
    }
    try {
      // Always resolved-only from the shell: in "value" mode main's first answer is its own
      // provisional default, which must not read as resolved here.
      const leave = joinSharedSubscription(bridge, name, (value) => {
        if (active && accepts(value)) onChange({ value, resolved: true, source: "shell" });
      });
      // A late consumer can unsubscribe while it hears the shell's last value, before this returns.
      if (active) unsubscribe = leave;
      else leave();
    } catch {
      settleOnFallback("subscribe_threw");
    }
  });

  return () => {
    active = false;
    unsubscribe?.();
    unsubscribe = null;
  };
}

/**
 * The shell's last answer for `name`, while another consumer on this page already holds it,
 * so a consumer mounted later starts from it rather than from its fallback; in a browser, its
 * override. Null otherwise.
 */
export function peekDesktopFeatureFlag<T>(
  name: string,
  {
    fallback,
    accepts = sameTypeAs(fallback),
  }: Pick<DesktopFeatureFlagOptions<T>, "fallback" | "accepts">,
): DesktopFeatureFlagState<T> | null {
  const bridge = featureFlagBridge();
  if (!bridge && !isNativeShell()) {
    const override = browserOverrideOf(name, accepts);
    return override ? { value: override.value, resolved: true } : null;
  }
  const latest = bridge ? sharedSubscriptions.get(bridge)?.get(name)?.latest : null;
  return latest && accepts(latest.value) ? { value: latest.value, resolved: true } : null;
}

/**
 * Subscribe to one desktop flag. `onChange` hears the provisional fallback at once in "value"
 * mode, then each resolved value. Returns an unsubscribe function.
 */
export function subscribeDesktopFeatureFlag<T>(
  name: string,
  options: DesktopFeatureFlagOptions<T>,
  onChange: (state: DesktopFeatureFlagState<T>) => void,
): () => void {
  return readDesktopFeatureFlag(name, options, ({ value, resolved }) =>
    onChange({ value, resolved }),
  );
}
