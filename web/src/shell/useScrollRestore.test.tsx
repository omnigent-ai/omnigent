// Tests for useScrollRestore — the shared scroll-position cache behind the
// Files panel and the file viewer:
//
//   1. Revisiting a key restores the offset saved before unmount.
//   2. A fresh key starts at the top, unaffected by another key's offset.
//   3. The loading clamp (scrollTop forced to 0 before the content is tall
//      enough) cannot overwrite the saved offset.
//   4. The restore keeps trying while the content is still growing, and only
//      gives up once its time budget expires.
//   5. Real user input (a wheel gesture) settles the restore immediately.
//   6. Saving resumes once the restore settles.
//   7. A null key disables persistence entirely.

import { useRef } from "react";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SCROLL_RESTORE_BUDGET_MS,
  getSavedScrollTop,
  saveScrollTop,
  useScrollRestore,
} from "./useScrollRestore";

function Scroller({ scrollKey, ready }: { scrollKey: string | null; ready: boolean }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const onScroll = useScrollRestore(ref, scrollKey, ready);
  return <div ref={ref} data-testid="scroller" onScroll={onScroll} />;
}

function mount(scrollKey: string | null, ready = true) {
  const view = render(<Scroller scrollKey={scrollKey} ready={ready} />);
  const el = view.getByTestId("scroller");
  return { view, el };
}

// The restore's budget is measured with performance.now(), so tests drive a
// controllable clock instead of waiting out real seconds.
let now = 0;

async function nextFrame() {
  await act(() => new Promise((resolve) => requestAnimationFrame(() => resolve(undefined))));
}

// jsdom has no layout (scrollHeight/clientHeight are 0), so a saved offset > 0
// is never reachable — the restore settles when the budget runs out.
async function settleRestore() {
  now += SCROLL_RESTORE_BUDGET_MS + 1;
  await nextFrame();
}

beforeEach(() => {
  now = 0;
  vi.spyOn(performance, "now").mockImplementation(() => now);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("useScrollRestore", () => {
  it("restores the saved offset when a key is revisited", async () => {
    const first = mount("view:a");
    await settleRestore();
    first.el.scrollTop = 120;
    fireEvent.scroll(first.el);
    first.view.unmount();

    const again = mount("view:a");
    expect(again.el.scrollTop).toBe(120);
  });

  it("starts a different key at the top", async () => {
    const first = mount("view:b");
    await settleRestore();
    first.el.scrollTop = 200;
    fireEvent.scroll(first.el);
    first.view.unmount();

    const other = mount("view:c");
    expect(other.el.scrollTop).toBe(0);
  });

  it("does not let the loading clamp overwrite the saved offset", () => {
    saveScrollTop("view:clamp", 90);

    // Revisit: the container is still a short placeholder, so the browser
    // clamps scrollTop to 0 and fires a scroll event.
    const { el } = mount("view:clamp");
    el.scrollTop = 0;
    fireEvent.scroll(el);

    expect(getSavedScrollTop("view:clamp")).toBe(90);
  });

  it("keeps retrying while the height is stable but the budget has not expired", async () => {
    saveScrollTop("view:slow", 140);
    const { el } = mount("view:slow");

    // Async content (highlighting, images, lazy cells) can stall for several
    // frames before growing; the restore must not surrender during the stall.
    now += 100;
    await nextFrame();
    await nextFrame();

    el.scrollTop = 0;
    fireEvent.scroll(el);
    expect(getSavedScrollTop("view:slow")).toBe(140);
  });

  it("settles as soon as the target is reachable", async () => {
    saveScrollTop("view:reachable", 50);
    const { el } = mount("view:reachable");
    // Tall content: the offset fits, so no budget needs to be spent.
    Object.defineProperty(el, "scrollHeight", { configurable: true, value: 1000 });
    await nextFrame();

    el.scrollTop = 60;
    fireEvent.scroll(el);
    expect(getSavedScrollTop("view:reachable")).toBe(60);
  });

  it("stops fighting the user when they scroll during a pending restore", async () => {
    saveScrollTop("view:wheel", 300);
    const { el } = mount("view:wheel");

    fireEvent.wheel(el);
    el.scrollTop = 12;
    fireEvent.scroll(el);

    expect(getSavedScrollTop("view:wheel")).toBe(12);
  });

  it("saves again once the restore has settled", async () => {
    saveScrollTop("view:settle", 90);
    const { el } = mount("view:settle");
    await settleRestore();

    el.scrollTop = 45;
    fireEvent.scroll(el);

    expect(getSavedScrollTop("view:settle")).toBe(45);
  });

  it("waits for `ready` before restoring", () => {
    saveScrollTop("view:pending", 70);
    const { el } = mount("view:pending", false);
    expect(el.scrollTop).toBe(0);
  });

  it("persists nothing when the key is null", async () => {
    const { el } = mount(null);
    await settleRestore();
    el.scrollTop = 30;
    fireEvent.scroll(el);

    expect(getSavedScrollTop("null")).toBeUndefined();
  });
});
