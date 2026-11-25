// Guards the @pierre/diffs patch (patches/@pierre__diffs@1.3.6.patch): CodeView tells scroll
// listeners which scrolls landed where its own correction put them. A version bump that drops
// the patch fails here, rather than silently bringing back the width restore's flake.

import { CodeView } from "@pierre/diffs";
import { afterEach, describe, expect, it } from "vitest";

/** The engine internals the patch touches, reached only by this guard. */
interface Internals {
  scrollHeight: number;
  scrollTop: number;
  ownScrollWrite: number | undefined;
  applyScrollFix: (target: number, synced: number, windowSpecs: unknown) => void;
}

const MAX_SCROLL = 9_500;

function mounted() {
  const root = document.createElement("div");
  let top = 0;
  // jsdom can't scroll: a scroller of 10,000px in a 500px view, clamped as a browser does.
  Object.defineProperty(root, "scrollTop", {
    configurable: true,
    get: () => top,
    set: (value: number) => {
      top = Math.max(0, Math.min(value, MAX_SCROLL));
    },
  });
  root.scrollTo = ((options: ScrollToOptions) => {
    root.scrollTop = options.top ?? root.scrollTop;
  }) as typeof root.scrollTo;
  root.getBoundingClientRect = () => ({ height: 500, top: 0, bottom: 500 }) as DOMRect;
  document.body.append(root);
  const view = new CodeView();
  view.setup(root);
  const internals = view as unknown as Internals;
  internals.scrollHeight = 10_000;
  const owned: (boolean | undefined)[] = [];
  view.subscribeToScroll((_scrollTop, _view, details) => owned.push(details?.engineOwned));
  return {
    root,
    view,
    internals,
    owned,
    /** The engine's own correction, from where it believes the view is. */
    engineWrite: (target: number, believed = internals.scrollTop) =>
      internals.applyScrollFix(target, believed, view.getWindowSpecs()),
    /** Someone else's scroll: a script, find in page or assistive technology. */
    foreignWrite: (target: number) => {
      root.scrollTop = target;
    },
    scrollEvent: () => root.dispatchEvent(new Event("scroll")),
  };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("CodeView's engine-owned scroll signal (patched)", () => {
  it("says a scroll is the view's own when it landed where its correction put it", () => {
    const view = mounted();
    view.engineWrite(1_000);
    view.scrollEvent();
    expect(view.owned).toEqual([true]);
  });

  it("says someone else's scroll isn't", () => {
    const view = mounted();
    view.foreignWrite(1_000);
    view.scrollEvent();
    expect(view.owned).toEqual([false]);
  });

  it("claims nothing for a write that didn't move the view, so a pending foreign scroll stays foreign", () => {
    const view = mounted();
    // The engine believes the view is at 100; a foreign jump to 200 hasn't reached it yet.
    view.internals.scrollTop = 100;
    view.foreignWrite(200);
    view.engineWrite(200, 100);
    view.scrollEvent();
    expect(view.owned).toEqual([false]);
  });

  it("claims nothing for a write clamped to where the view already is", () => {
    const view = mounted();
    view.foreignWrite(MAX_SCROLL);
    view.engineWrite(20_000, 0);
    view.scrollEvent();
    expect(view.owned).toEqual([false]);
  });

  it("compares several writes before one scroll by the last", () => {
    const view = mounted();
    view.engineWrite(1_000);
    view.engineWrite(2_000);
    view.scrollEvent();
    expect(view.owned).toEqual([true]);
  });

  it("says a foreign scroll that replaced the view's own write before the event isn't its own", () => {
    const view = mounted();
    view.engineWrite(1_000);
    view.foreignWrite(3_000);
    view.scrollEvent();
    expect(view.owned).toEqual([false]);
  });

  it("forgets its own write on reset, so a later scroll to the same place isn't its own", () => {
    const view = mounted();
    view.engineWrite(1_000);
    view.view.reset();
    expect(view.internals.ownScrollWrite).toBeUndefined();
  });

  it("uses up its own write on the scroll it caused", () => {
    const view = mounted();
    view.engineWrite(1_000);
    view.scrollEvent();
    view.scrollEvent();
    expect(view.owned).toEqual([true, false]);
  });
});
