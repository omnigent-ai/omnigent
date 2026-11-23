// Where the reader is in a rich Markdown diff, kept across a change in the preview's width. A
// re-wrap moves everything, and Pierre's own correction anchors past the whole preview, so the
// review puts the section the reader was at back where it was. Only width changes restore; the
// reader's own scrolling, input and every other placement take over from it.

/** The section at the top of the view, as the reader left it. */
export interface PreviewReadingAnchor {
  /** The rich file. */
  path: string;
  /** The rendered document's version: a new version's sections are other sections. */
  version: string;
  /** The section's index, as its `data-markdown-diff-section` attribute names it. */
  section: number;
  /** The section's top, from the top of the review's view, in pixels. */
  offset: number;
  /** The preview's width when this was read. */
  width: number;
  /**
   * The reader's place in the section's text, when the browser could say: a character offset,
   * counted as `textRange` counts, and that character's top from the view's top.
   */
  text?: { offset: number; top: number };
}

/** A width change's restore: the anchor to put back, and the token that owns finishing it. */
export interface PreviewWidthRestore {
  anchor: PreviewReadingAnchor;
  token: number;
}

export interface PreviewWidthAnchor {
  /** The view at rest: what's at its top now, or nothing when that isn't a rich diff's section. */
  capture: (reading: PreviewReadingAnchor | null) => void;
  /**
   * `path`'s preview is now `width` wide. Returns the anchor to put back, or null when there's
   * none: the first width seen, a height-only change, another preview, or another version.
   */
  widthChanged: (path: string, version: string | null, width: number) => PreviewWidthRestore | null;
  /**
   * The restore `token` owns is done, in place or as close as it got: the view rests there. A
   * token a newer width change or a reset has replaced changes nothing.
   */
  restored: (token: number) => void;
  /** The restore `token` owns gave way; an obsolete token changes nothing. */
  abandoned: (token: number) => void;
  /** The reader, another placement, or a new document took over: forget the anchor. */
  reset: () => void;
}

export function createPreviewWidthAnchor(): PreviewWidthAnchor {
  /** The last width each preview was seen at. */
  const widths = new Map<string, number>();
  let anchor: PreviewReadingAnchor | null = null;
  /** The pre-resize anchor while it's being put back; rapid further widths keep it. */
  let frozen: PreviewReadingAnchor | null = null;
  /** The token of the restore under way; any other token is obsolete. */
  let current = 0;

  return {
    capture(reading) {
      // A restore is under way: the shifted view the resize left mustn't replace what to restore.
      if (frozen) return;
      // Read at a width this preview hasn't been seen at yet: mid-resize, before it's reported.
      if (reading && widths.has(reading.path) && widths.get(reading.path) !== reading.width) return;
      anchor = reading;
    },

    widthChanged(path, version, width) {
      const before = widths.get(path);
      widths.set(path, width);
      if (before === undefined || width <= 0 || before === width) return null;
      const base = frozen ?? anchor;
      // Another preview's width: the reader isn't there, so nothing of theirs moves.
      if (!base || base.path !== path) return null;
      // A new version of the reader's own document: its sections are other sections.
      if (version === null || base.version !== version) {
        anchor = null;
        frozen = null;
        current += 1;
        return null;
      }
      frozen = base;
      current += 1;
      return { anchor: base, token: current };
    },

    restored(token) {
      if (token !== current || !frozen) return;
      anchor = { ...frozen, width: widths.get(frozen.path) ?? frozen.width };
      frozen = null;
    },

    abandoned(token) {
      if (token !== current) return;
      anchor = null;
      frozen = null;
    },

    reset() {
      anchor = null;
      frozen = null;
      current += 1;
    },
  };
}

/** Where a width restore's own last scroll left the view it scrolled. */
export interface OwnScrollWrite {
  instance: object;
  top: number;
}

/**
 * Whether a scroll is someone else's, such as a script's, find in page's or assistive
 * technology's, which takes the view from a width restore: neither the restore's own last write
 * nor the view's own correction put it where it is. Each scroll uses up the pending own write.
 */
export function isForeignScroll(
  own: { current: OwnScrollWrite | null },
  view: { getContainerElement: () => HTMLElement | undefined },
  details: { engineOwned?: boolean } | undefined,
): boolean {
  const pending = own.current;
  own.current = null;
  if (details?.engineOwned) return false;
  const container = view.getContainerElement();
  return !(
    pending &&
    pending.instance === view &&
    container &&
    Math.round(container.scrollTop) === pending.top
  );
}
