// Tests for useScrollRestore — the shared scroll-position cache behind the
// Files panel and the file viewer:
//
//   1. Revisiting a key restores the offset saved before unmount.
//   2. A fresh key starts at the top, unaffected by another key's offset.
//   3. The loading clamp (scrollTop forced to 0 before the content is tall
//      enough) cannot overwrite the saved offset.
//   4. Saving resumes once the restore settles.
//   5. A null key disables persistence entirely.

import { useRef } from "react";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { getSavedScrollTop, saveScrollTop, useScrollRestore } from "./useScrollRestore";

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

// jsdom has no layout (scrollHeight/clientHeight are 0), so a saved offset > 0
// is never "reachable" — the restore settles via the height-stopped-changing
// path after one animation frame.
async function settleRestore() {
  await act(() => new Promise((resolve) => requestAnimationFrame(() => resolve(undefined))));
}

afterEach(cleanup);

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

  it("does not let the loading clamp overwrite the saved offset", async () => {
    saveScrollTop("view:clamp", 90);

    // Revisit: the container is still a short placeholder, so the browser
    // clamps scrollTop to 0 and fires a scroll event.
    const { el } = mount("view:clamp");
    el.scrollTop = 0;
    fireEvent.scroll(el);

    expect(getSavedScrollTop("view:clamp")).toBe(90);
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
