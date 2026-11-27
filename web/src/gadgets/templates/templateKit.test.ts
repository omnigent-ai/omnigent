import { afterEach, describe, expect, it, vi } from "vitest";

import { GADGET_KIT_HELPERS } from "./templateKit";
import { WHERE_WAS_I_HTML } from "./whereWasITemplate";

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
    options: { limit: number; intervalMs?: number; walkEveryMs?: number; walkDelayMs?: number },
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

  it("finishes a walk slower than the poll, and delivers it with the newest first page", async () => {
    // Each page answers only when the test lets it, so polls arrive while the walk is reading.
    const { collection, poll, reads } = pagedCollection(3);
    const held: (() => void)[] = [];
    const list = collection.list.bind(collection);
    collection.list = (args: { cursor?: string }) =>
      new Promise<Page>((resolve) => {
        held.push(() => resolve(list(args)));
      });
    const calls: { ids: string[]; complete: boolean }[] = [];
    kit.watchEveryPage(
      collection,
      (items, complete) => calls.push({ ids: items.map((item) => item.id), complete }),
      { limit: 1 },
    );

    poll();
    await settle();
    expect(held).toHaveLength(1);
    // Newer snapshots during the walk neither restart it nor are lost.
    poll();
    poll();
    held.shift()!();
    await settle();
    poll();
    poll();
    held.shift()!();
    await settle();

    expect(reads).toEqual(["c1", "c2"]);
    expect(calls).toEqual([{ ids: ["r0", "r1", "r2"], complete: true }]);
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

describe("watchEveryPage polling budget", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /** A polled collection of `count` records, 100 a page, counting every bridge request. */
  function polledCollection(count: number, intervalMs: number, requests: { n: number }) {
    const page = (from: number): Page => ({
      items: Array.from({ length: Math.min(100, count - from) }, (_, n) => ({
        id: `r${from + n}`,
        version: "v1",
        value: {},
      })),
      nextCursor: from + 100 < count ? `c${from + 100}` : null,
    });
    return {
      watch(listener: (snapshot: Snapshot) => void) {
        const poll = () => {
          requests.n += 1;
          listener({ status: "ready", ...page(0) });
        };
        poll();
        setInterval(poll, intervalMs);
        return () => {};
      },
      list({ cursor }: { cursor?: string }): Promise<Page> {
        requests.n += 1;
        return Promise.resolve(page(Number(String(cursor).slice(1))));
      },
    };
  }

  it("keeps the template's long histories well inside the request budget", async () => {
    vi.useFakeTimers();
    // As the "Where was I?" template polls its run and dismissal histories.
    expect(WHERE_WAS_I_HTML).toContain("walkEveryMs: RADAR_WALK_EVERY_MS });");
    expect(WHERE_WAS_I_HTML).toContain("walkDelayMs: RADAR_WALK_EVERY_MS / 2 });");
    const requests = { n: 0 };
    kit.watchEveryPage(polledCollection(950, 4000, requests), () => {}, {
      intervalMs: 4000,
      limit: 100,
      walkEveryMs: 60_000,
    });
    kit.watchEveryPage(polledCollection(950, 8000, requests), () => {}, {
      intervalMs: 8000,
      limit: 100,
      walkEveryMs: 60_000,
      walkDelayMs: 30_000,
    });
    const minutes = 5;
    for (let second = 0; second < minutes * 60; second += 1) {
      // oxlint-disable-next-line no-await-in-loop -- each tick lets the pages resolve.
      await vi.advanceTimersByTimeAsync(1000);
    }
    // About 41 a minute, against 240 the bridge allows (and about 225 without spacing):
    // the other watches and the viewer's actions keep most of the budget.
    expect(requests.n / minutes).toBeLessThan(60);
  });
});
