import { describe, expect, it } from "vitest";
import {
  isForeignScroll,
  createPreviewWidthAnchor,
  type PreviewReadingAnchor,
} from "./previewWidthAnchor";

const reading = (extra: Partial<PreviewReadingAnchor> = {}): PreviewReadingAnchor => ({
  path: "docs/a.md",
  version: "v1",
  section: 600,
  offset: 0,
  width: 400,
  ...extra,
});

/** An anchor that has seen `docs/a.md` at 400 px and holds the reader at section 600. */
function settled() {
  const anchor = createPreviewWidthAnchor();
  expect(anchor.widthChanged("docs/a.md", "v1", 400)).toBeNull();
  anchor.capture(reading());
  return anchor;
}

describe("the preview width anchor", () => {
  it("puts back the section the reader was at when the preview's width changes", () => {
    const anchor = settled();
    expect(anchor.widthChanged("docs/a.md", "v1", 200)?.anchor).toEqual(reading());
  });

  it("never restores on the first width it sees, or a change that leaves the width as it was", () => {
    const anchor = createPreviewWidthAnchor();
    anchor.capture(reading());
    expect(anchor.widthChanged("docs/a.md", "v1", 400)).toBeNull();
    // Height-only: the same width reported again.
    expect(anchor.widthChanged("docs/a.md", "v1", 400)).toBeNull();
    expect(anchor.widthChanged("docs/a.md", "v1", 0)).toBeNull();
  });

  it("keeps the pre-resize anchor when the shifted view is read before the resize is reported", () => {
    const anchor = settled();
    // Pierre's own correction scrolls first, at the new width.
    anchor.capture(reading({ section: 900, offset: -4_000, width: 200 }));
    expect(anchor.widthChanged("docs/a.md", "v1", 200)?.anchor).toEqual(reading());
  });

  it("keeps the anchor from before the first of several quick width changes", () => {
    const anchor = settled();
    expect(anchor.widthChanged("docs/a.md", "v1", 300)?.anchor).toEqual(reading());
    // Reads during the restore don't replace it.
    anchor.capture(reading({ section: 700, width: 300 }));
    expect(anchor.widthChanged("docs/a.md", "v1", 250)?.anchor).toEqual(reading());
    expect(anchor.widthChanged("docs/a.md", "v1", 200)?.anchor).toEqual(reading());
  });

  it("follows the reader again once the anchor is back, at the new width", () => {
    const anchor = settled();
    const restore = anchor.widthChanged("docs/a.md", "v1", 200);
    anchor.restored(restore!.token);
    anchor.capture(reading({ section: 650, width: 200 }));
    expect(anchor.widthChanged("docs/a.md", "v1", 400)?.anchor).toEqual(
      reading({ section: 650, width: 200 }),
    );
  });

  it("restores only the preview the reader is in, and only the same version of it", () => {
    const anchor = settled();
    expect(anchor.widthChanged("docs/b.md", "v1", 200)).toBeNull();
    expect(anchor.widthChanged("docs/a.md", "v2", 200)).toBeNull();
    // A version change forgets it: the next width change has nothing to restore.
    expect(anchor.widthChanged("docs/a.md", "v1", 300)).toBeNull();
  });

  it("forgets the anchor when the reader or another placement takes over", () => {
    const anchor = settled();
    anchor.widthChanged("docs/a.md", "v1", 300);
    anchor.reset();
    expect(anchor.widthChanged("docs/a.md", "v1", 200)).toBeNull();
  });

  it("has nothing to restore when the view's top isn't in a rich diff", () => {
    const anchor = settled();
    anchor.capture(null);
    expect(anchor.widthChanged("docs/a.md", "v1", 200)).toBeNull();
  });

  it("lets only the newest restore finish: an older one completing late changes nothing", () => {
    const anchor = settled();
    const first = anchor.widthChanged("docs/a.md", "v1", 300)!;
    const second = anchor.widthChanged("docs/a.md", "v1", 200)!;
    expect(second.anchor).toEqual(reading());
    // The first restore's placement went stale; its late completion mustn't touch the second.
    anchor.abandoned(first.token);
    anchor.restored(first.token);
    // So a shifted read at the new width still can't replace what the second restores.
    anchor.capture(reading({ section: 900, width: 200 }));
    expect(anchor.widthChanged("docs/a.md", "v1", 250)?.anchor).toEqual(reading());
  });

  it("ignores a completion reported twice, and one after a reset", () => {
    const anchor = settled();
    const restore = anchor.widthChanged("docs/a.md", "v1", 300)!;
    anchor.restored(restore.token);
    anchor.capture(reading({ section: 650, width: 300 }));
    // A duplicate completion of the same, finished restore doesn't bring back its anchor.
    anchor.restored(restore.token);
    expect(anchor.widthChanged("docs/a.md", "v1", 200)?.anchor).toEqual(
      reading({ section: 650, width: 300 }),
    );
    const next = anchor.widthChanged("docs/a.md", "v1", 250)!;
    anchor.reset();
    anchor.restored(next.token);
    expect(anchor.widthChanged("docs/a.md", "v1", 300)).toBeNull();
  });

  it("forgets the anchor when the newest restore gives way", () => {
    const anchor = settled();
    const restore = anchor.widthChanged("docs/a.md", "v1", 300)!;
    anchor.abandoned(restore.token);
    expect(anchor.widthChanged("docs/a.md", "v1", 200)).toBeNull();
  });
});

describe("isForeignScroll", () => {
  const viewAt = (scrollTop: number) => {
    const container = { scrollTop } as HTMLElement;
    return { getContainerElement: () => container, container };
  };

  it("counts the view's own correction as not foreign", () => {
    const own = { current: null };
    expect(isForeignScroll(own, viewAt(500), { engineOwned: true })).toBe(false);
  });

  it("counts a scroll that landed where the restore's own write put it as not foreign", () => {
    const view = viewAt(1_200);
    const own = { current: { instance: view, top: 1_200 } };
    expect(isForeignScroll(own, view, { engineOwned: false })).toBe(false);
  });

  it("counts any other scroll as foreign, small or large", () => {
    const view = viewAt(1_201);
    expect(isForeignScroll({ current: { instance: view, top: 1_200 } }, view, undefined)).toBe(
      true,
    );
    expect(isForeignScroll({ current: null }, viewAt(40_000), { engineOwned: false })).toBe(true);
  });

  it("uses up the pending own write on every scroll, whichever kind it was", () => {
    const view = viewAt(1_200);
    for (const details of [{ engineOwned: true }, { engineOwned: false }, undefined]) {
      const own = { current: { instance: view, top: 1_200 } };
      isForeignScroll(own, view, details);
      expect(own.current).toBeNull();
    }
    const own = { current: { instance: view, top: 1_200 } };
    expect(isForeignScroll(own, view, undefined)).toBe(false);
    expect(isForeignScroll(own, view, undefined)).toBe(true);
  });

  it("never credits a write made to another view, as after the viewer changed", () => {
    const earlier = viewAt(1_200);
    const now = viewAt(1_200);
    expect(isForeignScroll({ current: { instance: earlier, top: 1_200 } }, now, undefined)).toBe(
      true,
    );
  });
});
