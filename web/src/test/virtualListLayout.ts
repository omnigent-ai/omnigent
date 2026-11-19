/** jsdom layout stubs that let the real `@tanstack/react-virtual` window list rows. */

const SCROLLER_WIDTH = 300;

const rect = (top: number, height: number, width: number) => ({
  top,
  left: 0,
  right: width,
  bottom: top + height,
  width,
  height,
  x: 0,
  y: top,
});

// Sizes the `isScroller` element and each `<li>` at its own scrolled `top`, stacking sized lists in
// document order; other elements stay zero-sized. Returns a function restoring the stubs.
export function stubVirtualListLayout({
  isScroller,
  viewportHeight,
  rowHeight,
  scrollIntoView,
}: {
  isScroller: (element: HTMLElement) => boolean;
  viewportHeight: number;
  rowHeight: number;
  scrollIntoView: (options?: boolean | ScrollIntoViewOptions) => void;
}): () => void {
  const restorers: (() => void)[] = [];
  const stub = <Key extends keyof HTMLElement>(key: Key, descriptor: PropertyDescriptor) => {
    const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, key);
    Object.defineProperty(HTMLElement.prototype, key, { configurable: true, ...descriptor });
    restorers.push(() => {
      if (original) Object.defineProperty(HTMLElement.prototype, key, original);
      else Reflect.deleteProperty(HTMLElement.prototype, key);
    });
  };
  const scrollerOf = (element: HTMLElement) => {
    let node = element.parentElement;
    while (node && !isScroller(node)) node = node.parentElement;
    return node;
  };
  const sizedLists = (root: HTMLElement) =>
    [...root.querySelectorAll<HTMLElement>("ul")].filter((list) => list.style.height !== "");
  const listHeight = (list: HTMLElement) => Number.parseFloat(list.style.height) || 0;
  const listOffset = (list: HTMLElement, scroller: HTMLElement) =>
    sizedLists(scroller)
      .filter(
        (above) =>
          !above.contains(list) &&
          (above.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
      )
      .reduce((offset, above) => offset + listHeight(above), 0);

  stub("clientHeight", {
    get(this: HTMLElement) {
      return isScroller(this) ? viewportHeight : 0;
    },
  });
  // The virtualizer measures rows by offsetHeight; keep it in step with their rects.
  stub("offsetHeight", {
    get(this: HTMLElement) {
      if (isScroller(this)) return viewportHeight;
      return this.tagName === "LI" ? rowHeight : 0;
    },
  });
  stub("offsetWidth", {
    get(this: HTMLElement) {
      return isScroller(this) ? SCROLLER_WIDTH : 0;
    },
  });
  stub("scrollHeight", {
    get(this: HTMLElement) {
      return sizedLists(this).reduce((height, list) => height + listHeight(list), 0);
    },
  });
  stub("getBoundingClientRect", {
    value(this: HTMLElement) {
      if (isScroller(this)) return rect(0, viewportHeight, SCROLLER_WIDTH);
      const isRow = this.tagName === "LI";
      const list = isRow ? this.parentElement : this.tagName === "UL" ? this : null;
      const scroller = scrollerOf(this);
      // Content moves up as its scroller scrolls; a row sits at its own offset in its list.
      const listTop = list && scroller ? listOffset(list, scroller) - scroller.scrollTop : 0;
      const rowTop = isRow ? Number.parseFloat(this.style.top || "0") : 0;
      return rect(listTop + rowTop, isRow ? rowHeight : 0, SCROLLER_WIDTH);
    },
  });
  const scrollTops = new WeakMap<HTMLElement, number>();
  stub("scrollTop", {
    get(this: HTMLElement) {
      return scrollTops.get(this) ?? 0;
    },
    set(this: HTMLElement, value: number) {
      scrollTops.set(this, Math.max(0, value));
    },
  });
  stub("scrollTo", {
    value(this: HTMLElement, options: ScrollToOptions) {
      this.scrollTop = options.top ?? this.scrollTop;
      this.dispatchEvent(new Event("scroll"));
    },
  });
  stub("scrollIntoView", { value: scrollIntoView });

  return () => {
    for (const restore of restorers.reverse()) restore();
  };
}
