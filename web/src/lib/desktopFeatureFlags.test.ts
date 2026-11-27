import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  BROWSER_FLAG_OVERRIDES_KEY,
  resetBrowserFlagOverridesForTests,
  setBrowserFlagOverride,
} from "./browserFeatureFlagOverrides";
import {
  registerBrowserFlagDefinitions,
  resetBrowserFlagDefinitionsForTests,
} from "./browserFlagDefinitions";
import {
  peekDesktopFeatureFlag,
  readDesktopFeatureFlag,
  subscribeDesktopFeatureFlag,
  type DesktopFeatureFlagReading,
  type DesktopFeatureFlagState,
} from "./desktopFeatureFlags";

type Callback = (value: unknown) => void;

/**
 * A shell whose flag bridge the test drives; `names` is what getDesktopFeatures lists. Like
 * main, a shell with a `limit` refuses subscriptions past it, and a refused one delivers nothing.
 */
function installShell({
  names,
  bridge = true,
  features = true,
  limit = Infinity,
}: {
  names?: string[];
  bridge?: boolean;
  features?: boolean;
  limit?: number;
} = {}) {
  const subscriptions: { name: string; mode: string; callback: Callback; ended: boolean }[] = [];
  const refused: string[] = [];
  const live = () => subscriptions.filter((subscription) => !subscription.ended);
  const subscribe = vi.fn((name: string, options: { mode: string }, callback: Callback) => {
    if (live().length >= limit) {
      refused.push(name);
      return () => {};
    }
    const subscription = { name, mode: options.mode, callback, ended: false };
    subscriptions.push(subscription);
    return () => {
      subscription.ended = true;
    };
  });
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    kind: "electron",
    ...(bridge ? { featureFlags: { subscribe } } : {}),
    ...(features
      ? { getDesktopFeatures: async () => (names ? { featureFlags: { names } } : {}) }
      : {}),
  };
  /** Deliver `value` to every live subscription to `name`. */
  const send = (name: string, value: unknown) => {
    for (const subscription of live()) if (subscription.name === name) subscription.callback(value);
  };
  return { subscribe, subscriptions, refused, live, send };
}

function collect<T>(name: string, fallback: T, mode?: "value" | "resolved") {
  const states: DesktopFeatureFlagState<T>[] = [];
  const unsubscribe = subscribeDesktopFeatureFlag(name, { fallback, mode }, (state) =>
    states.push(state),
  );
  // Ended after the test too, so no override listener or shell subscription outlives it.
  readers.push(unsubscribe);
  return { states, unsubscribe };
}

// Readings a test starts outside a helper, ended after it so none outlives the test.
const readers: (() => void)[] = [];

afterEach(() => {
  for (const stop of readers.splice(0)) stop();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
  delete (window as unknown as { omnigentNative?: unknown }).omnigentNative;
  window.localStorage.removeItem(BROWSER_FLAG_OVERRIDES_KEY);
  resetBrowserFlagOverridesForTests();
});

describe("subscribeDesktopFeatureFlag", () => {
  it("follows a capable shell from the provisional fallback, unresolved until it answers", async () => {
    const shell = installShell({ names: ["gate"] });
    const { states } = collect("gate", false);
    const readings: DesktopFeatureFlagReading<boolean>[] = [];
    readers.push(
      readDesktopFeatureFlag("gate", { fallback: false }, (reading) => readings.push(reading)),
    );
    expect(states).toEqual([{ value: false, resolved: false }]);
    await vi.waitFor(() => expect(shell.subscriptions).toHaveLength(1));
    expect(shell.subscriptions[0]).toMatchObject({ name: "gate", mode: "resolved" });
    // Subscribed with no answer yet: still provisional, though the fallback is what it'll say.
    expect(states).toEqual([{ value: false, resolved: false }]);
    expect(readings).toEqual([{ value: false, resolved: false, source: "pending" }]);
    shell.subscriptions[0].callback(false);
    shell.subscriptions[0].callback(true);
    expect(states).toEqual([
      { value: false, resolved: false },
      { value: false, resolved: true },
      { value: true, resolved: true },
    ]);
    expect(readings.at(-1)).toEqual({ value: true, resolved: true, source: "shell" });
  });

  it("shows nothing before the shell answers in resolved mode", async () => {
    const shell = installShell({ names: ["gate"] });
    const { states } = collect("gate", true, "resolved");
    expect(states).toEqual([]);
    await vi.waitFor(() => expect(shell.subscriptions).toHaveLength(1));
    expect(shell.subscriptions[0].mode).toBe("resolved");
    shell.subscriptions[0].callback(false);
    expect(states).toEqual([{ value: false, resolved: true }]);
  });

  it("ignores values of the wrong type", async () => {
    const shell = installShell({ names: ["limit"] });
    const { states } = collect("limit", 3, "resolved");
    await vi.waitFor(() => expect(shell.subscriptions).toHaveLength(1));
    shell.subscriptions[0].callback("7");
    shell.subscriptions[0].callback(7);
    expect(states).toEqual([{ value: 7, resolved: true }]);
  });

  it.each([
    ["a browser", () => {}],
    ["an older shell without the bridge", () => installShell({ names: ["gate"], bridge: false })],
    ["a shell that doesn't list the flag", () => installShell({ names: ["other"] })],
    ["a shell without the featureFlags field", () => installShell({})],
    ["a shell without getDesktopFeatures", () => installShell({ features: false })],
  ])("settles on the fallback in %s", async (_label, install) => {
    install();
    const { states } = collect("gate", true, "resolved");
    await vi.waitFor(() => expect(states).toEqual([{ value: true, resolved: true }]));
    const shell = (
      window as unknown as { omnigentDesktop?: { featureFlags?: { subscribe: unknown } } }
    ).omnigentDesktop;
    if (shell?.featureFlags) {
      expect(shell.featureFlags.subscribe).not.toHaveBeenCalled();
    }
  });

  it("never subscribes when unsubscribed before the shell answers, and ends a live one", async () => {
    const shell = installShell({ names: ["gate"] });
    const early = collect("gate", false);
    early.unsubscribe();
    const live = collect("gate", false);
    await vi.waitFor(() => expect(shell.subscriptions).toHaveLength(1));
    live.unsubscribe();
    expect(shell.subscriptions[0].ended).toBe(true);
    shell.subscriptions[0].callback(true);
    expect(live.states).toEqual([{ value: false, resolved: false }]);
    expect(early.states).toEqual([{ value: false, resolved: false }]);
  });

  it("falls back, and names why, when the bridge's subscribe throws", async () => {
    const shell = installShell({ names: ["gate"] });
    // Every subscribe throws, so each reader that tries falls back on its own.
    shell.subscribe.mockImplementation(() => {
      throw new Error("bridge gone");
    });
    const { states } = collect("gate", false, "resolved");
    const readings: DesktopFeatureFlagReading<boolean>[] = [];
    readers.push(
      readDesktopFeatureFlag("gate", { fallback: false, mode: "resolved" }, (reading) =>
        readings.push(reading),
      ),
    );
    await vi.waitFor(() => expect(states).toEqual([{ value: false, resolved: true }]));
    await vi.waitFor(() =>
      expect(readings).toEqual([{ value: false, resolved: true, source: "subscribe_threw" }]),
    );
  });

  it("refuses no flag a page reads, however many consumers read the others", async () => {
    // Main allows a window 32 live subscriptions; the Pull Requests page has more consumers.
    const busy = ["composer", "sidebar", "pull-requests"];
    const shell = installShell({ names: [...busy, "pull-requests-page"], limit: 32 });
    for (let index = 0; index < 33; index += 1) collect(busy[index % busy.length], false);
    const page = collect("pull-requests-page", false);
    await vi.waitFor(() =>
      expect(shell.subscribe).toHaveBeenCalledWith(
        "pull-requests-page",
        expect.anything(),
        expect.anything(),
      ),
    );
    shell.send("pull-requests-page", true);
    expect(page.states.at(-1)).toEqual({ value: true, resolved: true });
    expect(shell.refused).toEqual([]);
    expect(shell.live()).toHaveLength(busy.length + 1);
  });

  it("shares one subscription per flag, each consumer with its own fallback and types", async () => {
    const shell = installShell({ names: ["gate"] });
    const asBoolean = collect("gate", false);
    const asNumber = collect<number | null>("gate", null, "resolved");
    await vi.waitFor(() => expect(shell.subscriptions).toHaveLength(1));
    shell.send("gate", true);
    expect(asBoolean.states).toEqual([
      { value: false, resolved: false },
      { value: true, resolved: true },
    ]);
    // A null fallback accepts a value of any type.
    expect(asNumber.states).toEqual([{ value: true, resolved: true }]);
    const typed = collect("gate", 3, "resolved");
    shell.send("gate", 7);
    await vi.waitFor(() => expect(typed.states).toEqual([{ value: 7, resolved: true }]));
    expect(asBoolean.states.at(-1)).toEqual({ value: true, resolved: true });
  });

  it("gives a consumer that joins late the shell's last value", async () => {
    const shell = installShell({ names: ["gate"] });
    collect("gate", false);
    await vi.waitFor(() => expect(shell.subscriptions).toHaveLength(1));
    shell.send("gate", true);
    const late = collect("gate", false, "resolved");
    await vi.waitFor(() => expect(late.states).toEqual([{ value: true, resolved: true }]));
    expect(shell.subscribe).toHaveBeenCalledTimes(1);
  });

  it("ends the shell's subscription with its last consumer, and starts afresh after", async () => {
    const shell = installShell({ names: ["gate"] });
    const first = collect("gate", false);
    const second = collect("gate", false);
    await vi.waitFor(() => expect(shell.subscriptions).toHaveLength(1));
    shell.send("gate", true);
    first.unsubscribe();
    expect(shell.subscriptions[0].ended).toBe(false);
    shell.send("gate", false);
    expect(second.states.at(-1)).toEqual({ value: false, resolved: true });
    expect(first.states.at(-1)).toEqual({ value: true, resolved: true });
    second.unsubscribe();
    expect(shell.subscriptions[0].ended).toBe(true);
    // A new consumer asks the shell again rather than reading the ended subscription's value.
    const next = collect("gate", false, "resolved");
    await vi.waitFor(() => expect(shell.subscriptions).toHaveLength(2));
    expect(next.states).toEqual([]);
    shell.send("gate", true);
    expect(next.states).toEqual([{ value: true, resolved: true }]);
  });

  it("keeps one live subscription when a consumer leaves before the shell answers", async () => {
    const shell = installShell({ names: ["gate"] });
    const leaving = collect("gate", false);
    const staying = collect("gate", false);
    leaving.unsubscribe();
    await vi.waitFor(() => expect(shell.subscriptions).toHaveLength(1));
    shell.send("gate", true);
    expect(shell.live()).toHaveLength(1);
    expect(staying.states.at(-1)).toEqual({ value: true, resolved: true });
    expect(leaving.states).toEqual([{ value: false, resolved: false }]);
  });

  it("subscribes again through a replacement bridge", async () => {
    const before = installShell({ names: ["gate"] });
    collect("gate", false);
    await vi.waitFor(() => expect(before.subscriptions).toHaveLength(1));
    before.send("gate", true);
    const after = installShell({ names: ["gate"] });
    const fresh = collect("gate", false, "resolved");
    await vi.waitFor(() => expect(after.subscriptions).toHaveLength(1));
    expect(fresh.states).toEqual([]);
    after.send("gate", false);
    expect(fresh.states).toEqual([{ value: false, resolved: true }]);
  });

  it("keeps the provisional fallback when the shell refuses the subscription", async () => {
    const shell = installShell({ names: ["gate"], limit: 0 });
    const { states } = collect("gate", false);
    await vi.waitFor(() => expect(shell.refused).toEqual(["gate"]));
    expect(states).toEqual([{ value: false, resolved: false }]);
  });

  it("checks each consumer's own accepts against the shared value", async () => {
    const shell = installShell({ names: ["limit"] });
    const small = (value: unknown): value is number => typeof value === "number" && value < 10;
    const anyNumber = collect("limit", 0, "resolved");
    const smallOnly: DesktopFeatureFlagState<number>[] = [];
    subscribeDesktopFeatureFlag(
      "limit",
      { fallback: 0, mode: "resolved", accepts: small },
      (state) => smallOnly.push(state),
    );
    await vi.waitFor(() => expect(shell.subscriptions).toHaveLength(1));
    shell.send("limit", 40);
    shell.send("limit", 4);
    expect(anyNumber.states).toEqual([
      { value: 40, resolved: true },
      { value: 4, resolved: true },
    ]);
    expect(smallOnly).toEqual([{ value: 4, resolved: true }]);
  });

  it("hands a value the bridge delivers while subscribing to its first consumer and to later ones", async () => {
    const shell = installShell({ names: ["gate"] });
    shell.subscribe.mockImplementation((_name, _options, callback) => {
      callback(true);
      return () => {};
    });
    const first = collect("gate", false, "resolved");
    await vi.waitFor(() => expect(first.states).toEqual([{ value: true, resolved: true }]));
    const second = collect("gate", false, "resolved");
    await vi.waitFor(() => expect(second.states).toEqual([{ value: true, resolved: true }]));
    expect(shell.subscribe).toHaveBeenCalledTimes(1);
  });

  it("delivers to every consumer when one of them throws", async () => {
    const shell = installShell({ names: ["gate"] });
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    subscribeDesktopFeatureFlag("gate", { fallback: false, mode: "resolved" }, () => {
      throw new Error("consumer failed");
    });
    const healthy = collect("gate", false, "resolved");
    await vi.waitFor(() => expect(shell.subscriptions).toHaveLength(1));
    shell.send("gate", true);
    expect(healthy.states).toEqual([{ value: true, resolved: true }]);
    expect(error).toHaveBeenCalled();
    error.mockRestore();
  });

  it("ends the shell's subscription when a late consumer leaves as it hears the last value", async () => {
    const shell = installShell({ names: ["gate"] });
    const first = collect("gate", false);
    await vi.waitFor(() => expect(shell.subscriptions).toHaveLength(1));
    shell.send("gate", true);
    const heard: DesktopFeatureFlagState<boolean>[] = [];
    const leave = subscribeDesktopFeatureFlag(
      "gate",
      { fallback: false, mode: "resolved" },
      (state) => {
        heard.push(state);
        leave();
      },
    );
    await vi.waitFor(() => expect(heard).toEqual([{ value: true, resolved: true }]));
    first.unsubscribe();
    expect(shell.subscriptions[0].ended).toBe(true);
  });
});

describe("readDesktopFeatureFlag", () => {
  const FLAG = "chat-composer-codemirror";

  function read(mode?: "value" | "resolved") {
    const readings: DesktopFeatureFlagReading<boolean>[] = [];
    const unsubscribe = readDesktopFeatureFlag(FLAG, { fallback: false, mode }, (reading) =>
      readings.push(reading),
    );
    return { readings, unsubscribe };
  }

  it("names a shell without the bridge apart from a real Off", () => {
    installShell({ names: [FLAG], bridge: false });
    expect(read().readings).toEqual([{ value: false, resolved: true, source: "no_bridge" }]);
  });

  it("names a shell that can't list its features", async () => {
    installShell({ names: [FLAG], features: false });
    const { readings } = read();
    await vi.waitFor(() => expect(readings).toHaveLength(2));
    expect(readings).toEqual([
      { value: false, resolved: false, source: "pending" },
      { value: false, resolved: true, source: "features_unavailable" },
    ]);
  });

  it.each([
    ["lists no flags", undefined],
    ["doesn't list this one", ["another-flag"]],
  ])("names a shell that %s as not listed", async (_case, names) => {
    installShell({ names });
    const { readings } = read("resolved");
    await vi.waitFor(() => expect(readings).toHaveLength(1));
    expect(readings[0]).toEqual({ value: false, resolved: true, source: "not_listed" });
  });

  it("reports the shell's own Off as resolved from the shell", async () => {
    const shell = installShell({ names: [FLAG] });
    const { readings } = read("resolved");
    await vi.waitFor(() => expect(shell.live()).toHaveLength(1));
    shell.send(FLAG, false);
    expect(readings).toEqual([{ value: false, resolved: true, source: "shell" }]);
    shell.send(FLAG, true);
    expect(readings.at(-1)).toEqual({ value: true, resolved: true, source: "shell" });
  });

  it("logs a consumer that throws on a fallback the shell's features settle", async () => {
    installShell({ names: ["another-flag"] });
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    readDesktopFeatureFlag(FLAG, { fallback: false, mode: "resolved" }, () => {
      throw new Error("consumer failed");
    });
    await vi.waitFor(() => expect(error).toHaveBeenCalled());
    error.mockRestore();
  });

  it("shares one shell subscription with the flag's other readers", async () => {
    const shell = installShell({ names: [FLAG] });
    const reading = read("resolved");
    const state = collect(FLAG, false, "resolved");
    await vi.waitFor(() => expect(shell.subscribe).toHaveBeenCalledTimes(1));
    shell.send(FLAG, true);
    expect(reading.readings.at(-1)).toEqual({ value: true, resolved: true, source: "shell" });
    // The older state reader still hears only value and resolved.
    expect(state.states.at(-1)).toEqual({ value: true, resolved: true });
    expect(shell.subscribe).toHaveBeenCalledTimes(1);
  });
});

describe("a browser's own overrides", () => {
  const FLAG = "example-page-flag";
  // A build's definitions say which flags a browser may set.
  beforeEach(() => {
    registerBrowserFlagDefinitions([{ name: FLAG, type: "boolean", rendererVisible: true }]);
  });
  // Each read's subscription, ended after its test so no later test hears an earlier one.
  const reads: (() => void)[] = [];
  afterEach(() => {
    for (const unsubscribe of reads.splice(0)) unsubscribe();
    resetBrowserFlagDefinitionsForTests();
  });

  function read<T>(fallback: T, accepts?: (value: unknown) => value is T) {
    const readings: DesktopFeatureFlagReading<T>[] = [];
    const unsubscribe = readDesktopFeatureFlag(FLAG, { fallback, accepts }, (reading) =>
      readings.push(reading),
    );
    reads.push(unsubscribe);
    return { readings, unsubscribe };
  }

  it("reads a stored override once the build registers its definitions after the read", () => {
    resetBrowserFlagDefinitionsForTests();
    window.localStorage.setItem(BROWSER_FLAG_OVERRIDES_KEY, JSON.stringify({ [FLAG]: true }));
    const { readings, unsubscribe } = read(false);
    expect(readings).toEqual([{ value: false, resolved: true, source: "no_bridge" }]);
    registerBrowserFlagDefinitions([{ name: FLAG, type: "boolean", rendererVisible: true }]);
    expect(readings.at(-1)).toEqual({ value: true, resolved: true, source: "browser_override" });
    unsubscribe();
  });

  it("logs a consumer that throws, and keeps following the flag", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    let calls = 0;
    const unsubscribe = readDesktopFeatureFlag(FLAG, { fallback: false }, () => {
      calls += 1;
      if (calls === 1) throw new Error("consumer failed");
    });
    setBrowserFlagOverride(FLAG, true);
    expect(calls).toBe(2);
    expect(error).toHaveBeenCalled();
    error.mockRestore();
    unsubscribe();
  });

  it("reads the caller's fallback until this browser sets the flag", () => {
    expect(read(false).readings).toEqual([{ value: false, resolved: true, source: "no_bridge" }]);
    expect(peekDesktopFeatureFlag(FLAG, { fallback: false })).toBeNull();
  });

  it("follows an override live, and goes back to the fallback when it's cleared", () => {
    const { readings, unsubscribe } = read(false);
    setBrowserFlagOverride(FLAG, true);
    expect(readings.at(-1)).toEqual({ value: true, resolved: true, source: "browser_override" });
    expect(peekDesktopFeatureFlag(FLAG, { fallback: false })).toEqual({
      value: true,
      resolved: true,
    });
    setBrowserFlagOverride(FLAG, undefined);
    expect(readings.at(-1)).toEqual({ value: false, resolved: true, source: "no_bridge" });
    expect(readings).toHaveLength(3);
    unsubscribe();
    setBrowserFlagOverride(FLAG, true);
    expect(readings).toHaveLength(3);
  });

  it("starts from an override saved before the page loaded", () => {
    window.localStorage.setItem(BROWSER_FLAG_OVERRIDES_KEY, JSON.stringify({ [FLAG]: false }));
    expect(read(true).readings).toEqual([
      { value: false, resolved: true, source: "browser_override" },
    ]);
  });

  it("hears another tab's change", () => {
    const { readings } = read(false);
    window.localStorage.setItem(BROWSER_FLAG_OVERRIDES_KEY, JSON.stringify({ [FLAG]: true }));
    window.dispatchEvent(new StorageEvent("storage", { key: BROWSER_FLAG_OVERRIDES_KEY }));
    expect(readings.at(-1)).toEqual({ value: true, resolved: true, source: "browser_override" });
  });

  it("keeps the fallback where the caller doesn't accept the override's shape", () => {
    setBrowserFlagOverride(FLAG, true);
    const isNumber = (value: unknown): value is number => typeof value === "number";
    expect(read(3, isNumber).readings).toEqual([{ value: 3, resolved: true, source: "no_bridge" }]);
  });

  it("never reads overrides on a desktop shell, older or current", async () => {
    setBrowserFlagOverride(FLAG, true);
    installShell({ names: [FLAG], bridge: false });
    expect(read(false).readings).toEqual([{ value: false, resolved: true, source: "no_bridge" }]);
    const shell = installShell({ names: [FLAG] });
    const { readings } = read(false);
    await vi.waitFor(() => expect(shell.subscribe).toHaveBeenCalled());
    shell.send(FLAG, false);
    expect(readings.at(-1)).toEqual({ value: false, resolved: true, source: "shell" });
  });

  for (const kind of ["ios", "android"]) {
    it(`never reads overrides in the ${kind} app`, () => {
      setBrowserFlagOverride(FLAG, true);
      (window as unknown as { omnigentNative?: unknown }).omnigentNative = { kind };
      expect(read(false).readings).toEqual([{ value: false, resolved: true, source: "no_bridge" }]);
      expect(peekDesktopFeatureFlag(FLAG, { fallback: false })).toBeNull();
    });
  }
});
