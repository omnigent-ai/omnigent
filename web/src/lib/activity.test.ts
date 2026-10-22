import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ActivityModule from "./activity";

import {
  ACTIVITY_AGE_HEADER,
  FUTURE_SKEW_MS,
  SHARED_ACTIVITY_KEY,
  activityAgeSeconds,
  recordActivity,
  resetActivityForTests,
  setUserEventCheckForTests,
  startActivityTracking,
  withActivityAge,
} from "./activity";

const T0 = Date.UTC(2026, 0, 1);
const HOUR = 3_600_000;
const TTL = 8 * HOUR;

// jsdom marks every dispatched event untrusted; events wrapped in `real()`
// stand in for browser-generated (trusted) input.
const trustedEvents = new WeakSet<Event>();
function real<E extends Event>(event: E): E {
  trustedEvents.add(event);
  return event;
}

function setVisibility(state: DocumentVisibilityState): void {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: state });
}

/** Change visibility the way the browser does, firing a trusted event. */
function showOrHide(state: DocumentVisibilityState): void {
  setVisibility(state);
  document.dispatchEvent(real(new Event("visibilitychange")));
}

function ageHeader(): string | null {
  return new Headers(withActivityAge().headers).get(ACTIVITY_AGE_HEADER);
}

/** A fresh copy of the module (a new or reloaded tab) sharing this window's localStorage. */
async function openTab(): Promise<typeof ActivityModule> {
  vi.resetModules();
  return import("./activity");
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  resetActivityForTests();
  setUserEventCheckForTests((event) => trustedEvents.has(event));
  setVisibility("visible");
});

afterEach(() => {
  resetActivityForTests();
  vi.restoreAllMocks();
  vi.useRealTimers();
  setVisibility("visible");
});

describe("activityAgeSeconds", () => {
  it("reports nothing until the first interaction; loading the page is not one", () => {
    expect(activityAgeSeconds(T0 + 60_000)).toBeNull();

    recordActivity(T0);

    expect(activityAgeSeconds(T0 + 4_999)).toBe(4);
  });

  it("is never negative within the allowed clock skew", () => {
    recordActivity(T0);

    expect(activityAgeSeconds(T0 - (FUTURE_SKEW_MS - 1_000))).toBe(0);
  });
});

describe("recordActivity", () => {
  it("records at most once per second", () => {
    recordActivity(T0);
    recordActivity(T0 + 900);
    expect(activityAgeSeconds(T0 + 2_000)).toBe(2); // ignored: still measured from T0

    recordActivity(T0 + 1_000);
    expect(activityAgeSeconds(T0 + 2_000)).toBe(1);
  });
});

describe("startActivityTracking", () => {
  it.each(["pointerdown", "click", "keydown", "wheel", "touchstart"])(
    "treats a trusted %s as an interaction",
    (type) => {
      startActivityTracking();
      vi.setSystemTime(T0 + 60_000);

      window.dispatchEvent(real(new Event(type)));

      expect(activityAgeSeconds(T0 + 65_000)).toBe(5);
    },
  );

  it("does not count a programmatic scroll", () => {
    startActivityTracking();
    recordActivity(T0);
    const pane = document.createElement("div");
    document.body.append(pane);
    vi.setSystemTime(T0 + 60_000);

    window.dispatchEvent(real(new Event("scroll")));
    pane.dispatchEvent(real(new Event("scroll", { bubbles: true })));
    pane.scrollTop = 500;

    expect(activityAgeSeconds(T0 + 60_000)).toBe(60);
    pane.remove();
  });

  it("does not count a focus event targeting an element, even at the window", () => {
    startActivityTracking();
    recordActivity(T0);
    window.dispatchEvent(real(new FocusEvent("blur")));
    const input = document.createElement("input");
    document.body.append(input);
    vi.setSystemTime(T0 + 60_000);

    input.dispatchEvent(real(new FocusEvent("focus", { bubbles: true })));

    expect(activityAgeSeconds(T0 + 60_000)).toBe(60);
    input.remove();
  });

  it("does not count programmatic focus on an element", () => {
    startActivityTracking();
    recordActivity(T0);
    const input = document.createElement("input");
    document.body.append(input);
    vi.setSystemTime(T0 + 60_000);

    input.focus();

    expect(document.activeElement).toBe(input);
    expect(activityAgeSeconds(T0 + 60_000)).toBe(60);
    input.remove();
  });

  it("counts a trusted click with no pointerdown or keydown (assistive technology)", () => {
    startActivityTracking();
    const button = document.createElement("button");
    document.body.append(button);
    vi.setSystemTime(T0 + 60_000);

    button.dispatchEvent(real(new MouseEvent("click", { bubbles: true })));

    expect(activityAgeSeconds(T0 + 60_000)).toBe(0);
    button.remove();
  });

  it("counts trusted wheel, key and pointer input dispatched on page elements", () => {
    startActivityTracking();
    const button = document.createElement("button");
    document.body.append(button);
    for (const [offset, event] of [
      [60_000, new WheelEvent("wheel", { bubbles: true })],
      [120_000, new KeyboardEvent("keydown", { bubbles: true, key: "a" })],
      [180_000, new Event("pointerdown", { bubbles: true })],
    ] as const) {
      vi.setSystemTime(T0 + offset);
      button.dispatchEvent(real(event));
      expect(activityAgeSeconds(T0 + offset)).toBe(0);
    }
    button.remove();
  });

  it("counts returning to the window after it lost focus", () => {
    startActivityTracking();
    recordActivity(T0);
    vi.setSystemTime(T0 + 30_000);
    window.dispatchEvent(real(new FocusEvent("blur")));
    expect(activityAgeSeconds(T0 + 30_000)).toBe(30);

    vi.setSystemTime(T0 + 60_000);
    window.dispatchEvent(real(new FocusEvent("focus")));

    expect(activityAgeSeconds(T0 + 60_000)).toBe(0);
  });

  it("counts the tab becoming visible after being hidden, but not becoming hidden", () => {
    startActivityTracking();
    recordActivity(T0);
    vi.setSystemTime(T0 + 60_000);
    showOrHide("hidden");
    expect(activityAgeSeconds(T0 + 60_000)).toBe(60);

    vi.setSystemTime(T0 + 120_000);
    showOrHide("visible");
    expect(activityAgeSeconds(T0 + 120_000)).toBe(0);
  });

  it("ignores background timers and network activity", () => {
    startActivityTracking();
    recordActivity(T0);
    vi.advanceTimersByTime(HOUR);

    expect(activityAgeSeconds()).toBe(3_600);
  });

  it("throttles bursts of events", () => {
    startActivityTracking();
    vi.setSystemTime(T0 + 10_000);
    window.dispatchEvent(real(new Event("pointerdown")));
    vi.setSystemTime(T0 + 10_400);
    window.dispatchEvent(real(new Event("pointerdown")));

    expect(activityAgeSeconds(T0 + 11_300)).toBe(1); // from 10_000, not 10_400
  });
});

describe("script-generated events", () => {
  it("ignores untrusted input of every kind, so background requests keep the old age", () => {
    startActivityTracking();
    recordActivity(T0);
    const button = document.createElement("button");
    document.body.append(button);
    vi.setSystemTime(T0 + 60_000);

    for (const type of ["pointerdown", "click", "keydown", "wheel", "touchstart"]) {
      window.dispatchEvent(new Event(type));
      button.dispatchEvent(new Event(type, { bubbles: true }));
    }
    button.click();
    window.dispatchEvent(new FocusEvent("blur"));
    window.dispatchEvent(new FocusEvent("focus"));
    setVisibility("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    setVisibility("visible");
    document.dispatchEvent(new Event("visibilitychange"));

    vi.setSystemTime(T0 + 90_000);
    expect(ageHeader()).toBe("90");
    expect(window.localStorage.getItem(SHARED_ACTIVITY_KEY)).toBe(String(T0));
    button.remove();
  });

  it("an untrusted hide or blur does not arm a trusted show or focus", () => {
    startActivityTracking();
    recordActivity(T0);
    vi.setSystemTime(T0 + 60_000);

    window.dispatchEvent(new FocusEvent("blur"));
    window.dispatchEvent(real(new FocusEvent("focus")));
    setVisibility("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    showOrHide("visible");

    expect(activityAgeSeconds(T0 + 60_000)).toBe(60);
  });

  it("the production check rejects jsdom-dispatched (untrusted) events", () => {
    setUserEventCheckForTests(null);
    startActivityTracking();
    vi.setSystemTime(T0 + 60_000);

    window.dispatchEvent(new MouseEvent("click"));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "a" }));

    expect(ageHeader()).toBeNull();
  });
});

describe("focus and visibility at load or restore", () => {
  it("does not count the page starting out focused and visible", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const tab = await openTab();
    tab.setUserEventCheckForTests((event) => trustedEvents.has(event));
    tab.startActivityTracking();

    // The browser reports the initial state of a loaded or restored page.
    window.dispatchEvent(real(new FocusEvent("focus")));
    setVisibility("visible");
    document.dispatchEvent(real(new Event("visibilitychange")));

    expect(new Headers(tab.withActivityAge().headers).has(ACTIVITY_AGE_HEADER)).toBe(false);
    expect(setItem).not.toHaveBeenCalled();
    tab.resetActivityForTests();
  });

  it("does not count being shown again after a back/forward-cache restore", () => {
    startActivityTracking();
    recordActivity(T0);
    vi.setSystemTime(T0 + 60_000);

    // Leaving for the cache hides the page, then fires pagehide; restoring shows it.
    showOrHide("hidden");
    window.dispatchEvent(real(new Event("pagehide")));
    window.dispatchEvent(real(new Event("pageshow")));
    showOrHide("visible");
    expect(activityAgeSeconds(T0 + 60_000)).toBe(60);

    // A later real switch away and back still counts.
    vi.setSystemTime(T0 + 120_000);
    showOrHide("hidden");
    showOrHide("visible");
    expect(activityAgeSeconds(T0 + 120_000)).toBe(0);
  });
});

describe("withActivityAge", () => {
  it("omits the header before any interaction, so the request cannot renew", () => {
    const init = withActivityAge({ headers: { Accept: "application/json" } });

    const headers = new Headers(init.headers);
    expect(headers.has(ACTIVITY_AGE_HEADER)).toBe(false);
    expect(headers.get("Accept")).toBe("application/json");
  });

  it("adds the header and keeps the caller's other headers and options", () => {
    recordActivity(T0);
    const init = withActivityAge({
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });

    const headers = new Headers(init.headers);
    expect(init.method).toBe("POST");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get(ACTIVITY_AGE_HEADER)).toBe("0");
  });

  it("reports the current age", () => {
    recordActivity(T0);
    vi.setSystemTime(T0 + 42_000);

    expect(ageHeader()).toBe("42");
  });

  it("leaves a caller-supplied value alone", () => {
    const init = withActivityAge({ headers: { [ACTIVITY_AGE_HEADER]: "7" } });

    expect(new Headers(init.headers).get(ACTIVITY_AGE_HEADER)).toBe("7");
  });
});

describe("shared across tabs", () => {
  it("reports an interaction made in another tab", async () => {
    const tabA = await openTab();
    const tabB = await openTab();
    tabA.startActivityTracking();
    tabB.startActivityTracking();

    // Tab A gets input at +5 min but makes no request; tab B, with no input, polls.
    vi.setSystemTime(T0 + 300_000);
    tabA.recordActivity();
    vi.setSystemTime(T0 + 310_000);
    const header = new Headers(tabB.withActivityAge().headers).get(ACTIVITY_AGE_HEADER);

    expect(header).toBe("10");
    expect(tabA.activityAgeSeconds()).toBe(10);
    tabA.resetActivityForTests();
    tabB.resetActivityForTests();
  });

  it("reads a value another tab wrote under the shared key", () => {
    startActivityTracking();
    window.localStorage.setItem(SHARED_ACTIVITY_KEY, String(T0 + 50_000));

    expect(activityAgeSeconds(T0 + 60_000)).toBe(10);
  });

  it("uses this tab's own interaction when it is the newer one", () => {
    recordActivity(T0);
    window.localStorage.setItem(SHARED_ACTIVITY_KEY, String(T0 - HOUR));

    expect(activityAgeSeconds(T0 + 5_000)).toBe(5);
  });

  it("ignores a stored value that is not a number", () => {
    recordActivity(T0);
    window.localStorage.setItem(SHARED_ACTIVITY_KEY, "soon");

    expect(activityAgeSeconds(T0 + 60_000)).toBe(60);
  });

  it("only moves the shared value forward", () => {
    // Another tab recorded a moment later (its clock slightly ahead, within the skew).
    window.localStorage.setItem(SHARED_ACTIVITY_KEY, String(T0 + 63_000));

    recordActivity(T0 + 60_000); // older than what another tab shared
    expect(window.localStorage.getItem(SHARED_ACTIVITY_KEY)).toBe(String(T0 + 63_000));

    recordActivity(T0 + 120_000);
    expect(window.localStorage.getItem(SHARED_ACTIVITY_KEY)).toBe(String(T0 + 120_000));
  });

  it("never writes storage on load or on requests, only on interactions", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const tab = await openTab();

    tab.startActivityTracking();
    tab.withActivityAge();
    tab.withActivityAge();
    expect(setItem).not.toHaveBeenCalled();

    tab.recordActivity(T0 + 1_000);
    expect(setItem).toHaveBeenCalledOnce();
    tab.resetActivityForTests();
  });

  it("a reloaded tab reports the old shared interaction, not its load time", async () => {
    window.localStorage.setItem(SHARED_ACTIVITY_KEY, String(T0 - 2 * HOUR));
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const reloaded = await openTab();

    // Background polling only, as from a restored or auto-reloaded tab.
    const first = new Headers(reloaded.withActivityAge().headers).get(ACTIVITY_AGE_HEADER);
    vi.setSystemTime(T0 + 60_000);
    const later = new Headers(reloaded.withActivityAge().headers).get(ACTIVITY_AGE_HEADER);

    expect(first).toBe("7200");
    expect(later).toBe("7260");
    expect(setItem).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(SHARED_ACTIVITY_KEY)).toBe(String(T0 - 2 * HOUR));
    reloaded.resetActivityForTests();
  });

  it("falls back to per-tab tracking when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    startActivityTracking();
    recordActivity(T0 + 30_000);

    expect(activityAgeSeconds(T0 + 45_000)).toBe(15);
    vi.setSystemTime(T0 + 45_000);
    expect(ageHeader()).toBe("15");
  });
});

describe("future timestamps (clock set back)", () => {
  it("trusts a shared time within the skew, as age 0", () => {
    window.localStorage.setItem(SHARED_ACTIVITY_KEY, String(T0 + FUTURE_SKEW_MS));

    expect(activityAgeSeconds(T0)).toBe(0);
  });

  it("treats a shared time beyond the skew as invalid: no header", () => {
    window.localStorage.setItem(SHARED_ACTIVITY_KEY, String(T0 + FUTURE_SKEW_MS + 1));

    expect(activityAgeSeconds(T0)).toBeNull();
    expect(ageHeader()).toBeNull();
  });

  it("stays invalid after the clock catches up, until real input", async () => {
    startActivityTracking();
    // The user was active at T0 + 10 h; then the clock is set back 10 h.
    const future = T0 + 10 * HOUR;
    vi.setSystemTime(future);
    window.dispatchEvent(real(new Event("pointerdown")));
    expect(window.localStorage.getItem(SHARED_ACTIVITY_KEY)).toBe(String(future));
    vi.setSystemTime(T0);

    // Background polling only, well past the catch-up point (future) plus the TTL.
    const catchUp = [future - FUTURE_SKEW_MS, future - 1, future, future + 1];
    const hourly = Array.from({ length: 20 }, (_, i) => T0 + i * HOUR);
    for (const t of [...hourly, ...catchUp, future + TTL, future + TTL + HOUR].sort(
      (a, b) => a - b,
    )) {
      vi.setSystemTime(t);
      expect(ageHeader(), `at T0 + ${(t - T0) / HOUR} h`).toBeNull();
    }
    // The invalid value was removed, so a tab opened after the catch-up agrees.
    expect(window.localStorage.getItem(SHARED_ACTIVITY_KEY)).toBeNull();
    const lateTab = await openTab();
    expect(new Headers(lateTab.withActivityAge().headers).has(ACTIVITY_AGE_HEADER)).toBe(false);
    lateTab.resetActivityForTests();

    // A real interaction replaces it, and ages resume from it.
    const now = future + TTL + 2 * HOUR;
    vi.setSystemTime(now);
    window.dispatchEvent(real(new KeyboardEvent("keydown", { key: "a" })));
    expect(window.localStorage.getItem(SHARED_ACTIVITY_KEY)).toBe(String(now));
    expect(ageHeader()).toBe("0");
    vi.setSystemTime(now + 30_000);
    expect(ageHeader()).toBe("30");
  });

  it("refuses a rejected shared value past catch-up even when storage can't be cleaned", () => {
    const future = T0 + 10 * HOUR;
    window.localStorage.setItem(SHARED_ACTIVITY_KEY, String(future));
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });

    expect(activityAgeSeconds(T0)).toBeNull(); // detected: too far ahead
    expect(window.localStorage.getItem(SHARED_ACTIVITY_KEY)).toBe(String(future));
    expect(activityAgeSeconds(future)).toBeNull();
    expect(activityAgeSeconds(future + TTL)).toBeNull();

    // A newer value from another tab's real input is accepted.
    window.localStorage.setItem(SHARED_ACTIVITY_KEY, String(future + TTL));
    expect(activityAgeSeconds(future + TTL + 20_000)).toBe(20);
  });

  it("keeps a future tab-local value invalid past catch-up without storage", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const future = T0 + 10 * HOUR;
    recordActivity(future);

    expect(activityAgeSeconds(T0)).toBeNull();
    expect(activityAgeSeconds(future)).toBeNull();
    expect(activityAgeSeconds(future + TTL)).toBeNull();

    recordActivity(future + TTL);
    expect(activityAgeSeconds(future + TTL + 5_000)).toBe(5);
  });

  it("lets a real interaction replace an invalid stored value from another tab", () => {
    window.localStorage.setItem(SHARED_ACTIVITY_KEY, String(T0 + 24 * HOUR));

    recordActivity(T0);

    expect(window.localStorage.getItem(SHARED_ACTIVITY_KEY)).toBe(String(T0));
    expect(activityAgeSeconds(T0 + 3_000)).toBe(3);
  });
});
