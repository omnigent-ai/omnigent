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

/**
 * Give the element `isScroller` matches a viewport and every `<li>` a fixed height at its
 * own `top`, scrolled with its scroller; everything else stays zero-sized. Returns a
 * function that restores the original prototype properties.
 */
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
  // Every list starts at the content's top, so the tallest sized one spans the content.
  stub("scrollHeight", {
    get(this: HTMLElement) {
      const heights = [...this.querySelectorAll<HTMLElement>("ul")].map(
        (list) => Number.parseFloat(list.style.height) || 0,
      );
      return Math.max(0, ...heights);
    },
  });
  stub("getBoundingClientRect", {
    value(this: HTMLElement) {
      if (isScroller(this)) return rect(0, viewportHeight, SCROLLER_WIDTH);
      const isRow = this.tagName === "LI";
      // Content moves up as its scroller scrolls; a row sits at its own offset in the list.
      const scrolledBy = scrollerOf(this)?.scrollTop ?? 0;
      const rowTop = isRow ? Number.parseFloat(this.style.top || "0") : 0;
      const top = (this.tagName === "UL" || isRow ? -scrolledBy : 0) + rowTop;
      return rect(top, isRow ? rowHeight : 0, SCROLLER_WIDTH);
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
