import { describe, expect, it } from "vitest";

import { GADGET_KIT_HELPERS } from "./templateKit";

const plain = new Function(`${GADGET_KIT_HELPERS}; return plain;`)() as (value: unknown) => string;

describe("plain", () => {
  it("drops Markdown marks from text a session wrote", () => {
    expect(plain("Choose **approve** or `request changes`")).toBe(
      "Choose approve or request changes",
    );
    expect(plain("an _emphasis_ and __strong__ word")).toBe("an emphasis and strong word");
    expect(plain("see [the PR](https://example.com/1)")).toBe("see the PR");
  });

  it("keeps the underscores in snake_case names", () => {
    expect(plain("Ramp smart_reminders_enabled to 10%")).toBe(
      "Ramp smart_reminders_enabled to 10%",
    );
    expect(plain("`dark_mode_v2_enabled` and __init__")).toBe("dark_mode_v2_enabled and init");
  });
});

interface Doc {
  id: string;
  version: string;
  value: unknown;
}
interface Page {
  items: Doc[];
  nextCursor: string | null;
}
interface Snapshot {
  status: string;
  items: Doc[];
  nextCursor: string | null;
}

const kit = new Function(
  `${GADGET_KIT_HELPERS}; return { watchEveryPage, MAX_EXTRA_PAGES };`,
)() as {
  watchEveryPage: (
    collection: unknown,
    handler: (items: Doc[], complete: boolean) => void,
    options: { limit: number },
  ) => unknown;
  MAX_EXTRA_PAGES: number;
};

/** A collection whose records sit one per page, `count` pages in all, and a watch to poll. */
function pagedCollection(count: number) {
  const doc = (n: number): Doc => ({ id: `r${n}`, version: "v1", value: { n } });
  const cursor = (n: number) => (n < count ? `c${n}` : null);
  let watcher: ((snapshot: Snapshot) => void) | null = null;
  const reads: (string | undefined)[] = [];
  const collection = {
    watch(listener: (snapshot: Snapshot) => void) {
      watcher = listener;
      return () => {};
    },
    list({ cursor: from }: { cursor?: string }): Promise<Page> {
      reads.push(from);
      const n = Number(String(from).slice(1));
      return Promise.resolve({ items: [doc(n)], nextCursor: cursor(n + 1) });
    },
  };
  const poll = () => watcher!({ status: "ready", items: [doc(0)], nextCursor: cursor(1) });
  return { collection, poll, reads };
}

const settle = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

describe("watchEveryPage", () => {
  it("walks a long collection over several polls instead of starting again each time", async () => {
    // Eleven pages: more than one poll may read, so the second poll goes on from the first's end.
    const { collection, poll, reads } = pagedCollection(11);
    const calls: { ids: string[]; complete: boolean }[] = [];
    kit.watchEveryPage(
      collection,
      (items, complete) => calls.push({ ids: items.map((item) => item.id), complete }),
      { limit: 1 },
    );

    poll();
    await settle();
    expect(reads).toHaveLength(kit.MAX_EXTRA_PAGES);
    expect(calls.at(-1)).toEqual({
      ids: Array.from({ length: kit.MAX_EXTRA_PAGES + 1 }, (_, n) => `r${n}`),
      complete: false,
    });

    poll();
    await settle();
    // The second poll read only what was left, and the walk is complete.
    expect(reads.slice(kit.MAX_EXTRA_PAGES)).toEqual(["c10"]);
    expect(calls.at(-1)).toEqual({
      ids: Array.from({ length: 11 }, (_, n) => `r${n}`),
      complete: true,
    });

    // A finished walk starts again from the first page on the next poll.
    poll();
    await settle();
    expect(reads[kit.MAX_EXTRA_PAGES + 1]).toBe("c1");
  });
});

describe("keyed", () => {
  const helpers = new Function(`${GADGET_KIT_HELPERS}; return { keyed, el };`)() as {
    keyed: (
      parent: HTMLElement,
      items: string[],
      keyOf: (item: string) => string,
      build: (item: string) => HTMLElement,
    ) => HTMLElement[];
    el: (tag: string, props?: Record<string, unknown>) => HTMLElement;
  };

  it("fades a row in once, not again when its parent is rebuilt on the next draw", () => {
    const fades: string[] = [];
    const original = Element.prototype.animate;
    Element.prototype.animate = function (this: Element, frames: Keyframe[]) {
      if (frames[0] && "opacity" in frames[0]) fades.push(this.getAttribute("data-key") ?? "");
      return {} as Animation;
    } as typeof Element.prototype.animate;
    try {
      const draw = () => {
        // A design that builds its lanes afresh on every draw, then its rows inside them.
        const lane = helpers.el("div");
        helpers.keyed(
          lane,
          ["row-a", "row-b"],
          (item) => item,
          (item) => helpers.el("p", { text: item }),
        );
      };
      draw();
      expect(fades).toEqual(["row-a", "row-b"]);
      draw();
      draw();
      // Drawn moments ago: still the same rows, so they stay visible instead of fading again.
      expect(fades).toEqual(["row-a", "row-b"]);
    } finally {
      Element.prototype.animate = original;
    }
  });
});
