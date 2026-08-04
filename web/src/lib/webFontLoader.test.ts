import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type FontCatalogEntry, getFontById } from "./fontCatalog";
import { loadFont, loadFontByFamily, resetFontLoaderForTests } from "./webFontLoader";

// A resolvable promise handle for driving readiness/link ordering in tests.
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

// Records each `document.fonts.load(spec)` call and hands back a deferred whose
// resolution the test controls, so readiness ordering is observable rather than
// resolving instantly.
type FontsLoadCall = { spec: string; deferred: ReturnType<typeof deferred<unknown[]>> };
let fontsLoadCalls: FontsLoadCall[] = [];

beforeEach(() => {
  fontsLoadCalls = [];
  Object.defineProperty(document, "fonts", {
    configurable: true,
    value: {
      load: vi.fn((spec: string) => {
        const d = deferred<unknown[]>();
        fontsLoadCalls.push({ spec, deferred: d });
        return d.promise;
      }),
    },
  });
});

afterEach(() => {
  // resetFontLoaderForTests removes the nodes it injected, so no hand-cleanup.
  resetFontLoaderForTests();
  vi.restoreAllMocks();
});

/** Injected stylesheet <link> nodes (jsdom won't fire their load event itself). */
function links(): HTMLLinkElement[] {
  return [...document.querySelectorAll<HTMLLinkElement>(`link[data-omnigent-font]`)];
}
/** Injected @font-face <style> nodes. */
function styles(): HTMLStyleElement[] {
  return [...document.querySelectorAll<HTMLStyleElement>(`style[data-omnigent-font]`)];
}
/** Fire the `load` event on every injected link, as a browser would on parse. */
function fireLinkLoads(): void {
  for (const link of links()) link.dispatchEvent(new Event("load"));
}
/** Let queued microtasks/promise callbacks run. */
const flush = () => new Promise<void>((r) => setTimeout(r, 0));

const inter = () => getFontById("inter") as FontCatalogEntry;
const roboto = () => getFontById("roboto") as FontCatalogEntry;
const nerd = () => getFontById("jetbrainsmono-nerd-font-mono") as FontCatalogEntry;
const cascadia = () => getFontById("cascadia-code") as FontCatalogEntry;

describe("webFontLoader — google-css2 readiness ordering", () => {
  it("does NOT query document.fonts until the stylesheet <link> has loaded", async () => {
    const p = loadFont(inter());
    await flush();
    // Link is injected, but its `load` event hasn't fired — readiness must not
    // have been queried yet (that's the race the fix closes).
    expect(links().length).toBe(1);
    expect(fontsLoadCalls.length).toBe(0);

    // Now the stylesheet parses.
    fireLinkLoads();
    await flush();
    expect(fontsLoadCalls.length).toBe(1);
    expect(fontsLoadCalls[0].spec).toBe(`16px "Inter"`);

    // Still pending until the FontFaceSet genuinely settles non-empty.
    let settled = false;
    void p.then(() => {
      settled = true;
    });
    await flush();
    expect(settled).toBe(false);

    fontsLoadCalls[0].deferred.resolve([{}]); // one matched FontFace
    await expect(p).resolves.toBe(true);
  });

  it("resolves false when the FontFaceSet matches nothing (rules not registered)", async () => {
    const p = loadFont(roboto());
    await flush();
    fireLinkLoads();
    await flush();
    // Empty array = not ready → false, so no premature remeasure fires.
    fontsLoadCalls[0].deferred.resolve([]);
    await expect(p).resolves.toBe(false);
  });

  it("resolves false and does NOT query fonts when the <link> errors", async () => {
    const p = loadFont(inter());
    await flush();
    links()[0].dispatchEvent(new Event("error"));
    await expect(p).resolves.toBe(false);
    expect(fontsLoadCalls.length).toBe(0);
  });

  it("never injects the same stylesheet twice", async () => {
    const p1 = loadFont(inter());
    const p2 = loadFont(inter());
    expect(p1).toBe(p2); // shared in-flight promise
    await flush();
    fireLinkLoads();
    await flush();
    fontsLoadCalls.forEach((c) => c.deferred.resolve([{}]));
    await Promise.all([p1, p2]);
    expect(links().length).toBe(1);
    expect(fontsLoadCalls.length).toBe(1);
  });
});

describe("webFontLoader — dedup by resource identity, not id", () => {
  it("dedupes two DIFFERENT ids that share one stylesheet URL", async () => {
    // The two IBM Plex Mono entries have distinct ids but identical Google CSS2
    // URLs — they must inject once and share one in-flight promise.
    const fixed = getFontById("ibm-plex-mono") as FontCatalogEntry;
    const code = getFontById("ibm-plex-mono-code") as FontCatalogEntry;
    expect(fixed.id).not.toBe(code.id);

    const pa = loadFont(fixed);
    const pb = loadFont(code);
    expect(pa).toBe(pb);
    await flush();
    expect(links().length).toBe(1);

    fireLinkLoads();
    await flush();
    expect(fontsLoadCalls.length).toBe(1);
    fontsLoadCalls[0].deferred.resolve([{}]);
    await expect(pa).resolves.toBe(true);
    await expect(pb).resolves.toBe(true);
  });

  it("retries after a <link> error rather than caching the failure", async () => {
    const p1 = loadFont(inter());
    await flush();
    links()[0].dispatchEvent(new Event("error"));
    await expect(p1).resolves.toBe(false);
    await flush();

    // A fresh call must re-inject (previous promise resolved false → evicted).
    const p2 = loadFont(inter());
    expect(p2).not.toBe(p1);
    await flush();
    expect(links().length).toBe(1);
    fireLinkLoads();
    await flush();
    fontsLoadCalls.at(-1)!.deferred.resolve([{}]);
    await expect(p2).resolves.toBe(true);
  });
});

describe("webFontLoader — failure removes the injected node so retry re-fetches", () => {
  it("google-css2: fonts.load REJECTS → <link> removed, next call reinjects and re-queries", async () => {
    const p1 = loadFont(inter());
    await flush();
    fireLinkLoads();
    await flush();
    expect(links().length).toBe(1);
    // The stylesheet parsed but document.fonts.load rejects (face errored).
    fontsLoadCalls[0].deferred.reject(new Error("network"));
    await expect(p1).resolves.toBe(false);
    await flush();
    // Regression guard: the dead <link> must be gone, or the DOM guard would
    // skip reinjection forever.
    expect(links().length).toBe(0);

    // Retry: reinjects a fresh link AND issues a fresh document.fonts.load.
    const p2 = loadFont(inter());
    expect(p2).not.toBe(p1);
    await flush();
    expect(links().length).toBe(1);
    fireLinkLoads();
    await flush();
    expect(fontsLoadCalls.length).toBe(2);
    fontsLoadCalls[1].deferred.resolve([{}]);
    await expect(p2).resolves.toBe(true);
  });

  it("google-css2: fonts.load resolves EMPTY → <link> removed, next call reinjects", async () => {
    const p1 = loadFont(roboto());
    await flush();
    fireLinkLoads();
    await flush();
    fontsLoadCalls[0].deferred.resolve([]); // not-ready
    await expect(p1).resolves.toBe(false);
    await flush();
    expect(links().length).toBe(0);

    const p2 = loadFont(roboto());
    expect(p2).not.toBe(p1);
    await flush();
    expect(links().length).toBe(1);
    fireLinkLoads();
    await flush();
    fontsLoadCalls.at(-1)!.deferred.resolve([{}]);
    await expect(p2).resolves.toBe(true);
  });

  it("self-hosted: not-ready → <style> removed, next call reinjects a fresh face", async () => {
    const p1 = loadFont(nerd());
    await flush();
    expect(styles().length).toBe(1);
    fontsLoadCalls[0].deferred.resolve([]); // errored/not-ready face
    await expect(p1).resolves.toBe(false);
    await flush();
    // The errored @font-face <style> is dropped so a retry mints a fresh face
    // instead of document.fonts.load reusing the rejected one.
    expect(styles().length).toBe(0);

    const p2 = loadFont(nerd());
    expect(p2).not.toBe(p1);
    await flush();
    expect(styles().length).toBe(1);
    fontsLoadCalls.at(-1)!.deferred.resolve([{}]);
    await expect(p2).resolves.toBe(true);
  });

  it("genuine success after a prior failure works end-to-end", async () => {
    const p1 = loadFont(inter());
    await flush();
    fireLinkLoads();
    await flush();
    fontsLoadCalls[0].deferred.resolve([]); // fail once
    await expect(p1).resolves.toBe(false);
    await flush();

    const p2 = loadFont(inter());
    await flush();
    fireLinkLoads();
    await flush();
    fontsLoadCalls.at(-1)!.deferred.resolve([{}]); // succeed on retry
    await expect(p2).resolves.toBe(true);
    expect(links().length).toBe(1);
  });

  it("unsupported document.fonts API is NOT treated as retryable (no node thrash)", async () => {
    // Simulate an environment without the FontFaceSet API.
    Object.defineProperty(document, "fonts", { configurable: true, value: undefined });
    const p1 = loadFont(nerd());
    await flush();
    // The @font-face was injected (CSS may still paint via font-display); it is
    // NOT removed, because "API missing" isn't an asset failure.
    expect(styles().length).toBe(1);
    await expect(p1).resolves.toBe(false);
    await flush();
    expect(styles().length).toBe(1);
  });
});

describe("webFontLoader — self-hosted", () => {
  it("injects @font-face rules once for a Nerd Font and awaits readiness", async () => {
    const p = loadFont(nerd());
    await flush();
    const style = styles()[0];
    expect(style?.textContent).toContain("@font-face");
    expect(style?.textContent).toContain(`font-family: 'JetBrainsMono Nerd Font Mono'`);
    expect(style?.textContent).toContain(nerd().faces?.[0].url ?? "MISSING");
    // Self-hosted has no <link> to await — readiness is queried straight away.
    expect(fontsLoadCalls[0].spec).toBe(`16px "JetBrainsMono Nerd Font Mono"`);
    fontsLoadCalls[0].deferred.resolve([{}]);
    await expect(p).resolves.toBe(true);
  });

  it("injects one @font-face per declared face", async () => {
    const p = loadFont(cascadia());
    await flush();
    const faceCount = styles()[0]?.textContent?.match(/@font-face/g)?.length ?? 0;
    expect(faceCount).toBe(cascadia().faces?.length);
    fontsLoadCalls[0].deferred.resolve([{}]);
    await p;
  });
});

describe("webFontLoader — no-ops", () => {
  it("does not fetch a bundled font (resolves ready)", async () => {
    await expect(loadFont(getFontById("geist-mono") as FontCatalogEntry)).resolves.toBe(true);
    expect(links().length + styles().length).toBe(0);
    expect(fontsLoadCalls.length).toBe(0);
  });

  it("does not fetch the empty system-default family", async () => {
    await expect(loadFont(getFontById("system-ui") as FontCatalogEntry)).resolves.toBe(true);
    expect(fontsLoadCalls.length).toBe(0);
  });
});

describe("webFontLoader — resetFontLoaderForTests cleans up the DOM", () => {
  it("removes injected <link> and <style> nodes it owns", async () => {
    const pLink = loadFont(inter()); // injects a <link>
    const pStyle = loadFont(nerd()); // injects a <style>
    await flush();
    // Let the stylesheet parse + both readiness probes resolve so the loads
    // settle before we reset (rather than leaving promises dangling).
    fireLinkLoads();
    await flush();
    fontsLoadCalls.forEach((c) => c.deferred.resolve([{}]));
    await Promise.all([pLink, pStyle]);
    expect(links().length).toBe(1);
    expect(styles().length).toBe(1);

    resetFontLoaderForTests();
    // The nodes the loader tracked are gone without any hand-removal.
    expect(document.querySelectorAll("[data-omnigent-font]").length).toBe(0);
  });
});

describe("webFontLoader — loadFontByFamily bridge", () => {
  it("loads a catalog family typed as a bare name", async () => {
    const { entry, ready } = loadFontByFamily("Fira Code");
    expect(entry?.id).toBe("fira-code");
    await flush();
    fireLinkLoads();
    await flush();
    fontsLoadCalls.at(-1)!.deferred.resolve([{}]);
    await expect(ready).resolves.toBe(true);
  });

  it("routes a shared family to the requested category", async () => {
    const { entry } = loadFontByFamily("IBM Plex Mono", "code");
    expect(entry?.id).toBe("ibm-plex-mono-code");
    expect(entry?.category).toBe("code");
  });

  it("resolves false without loading for a non-catalog family", async () => {
    const { entry, ready } = loadFontByFamily("Comic Sans MS");
    expect(entry).toBeUndefined();
    await expect(ready).resolves.toBe(false);
    expect(fontsLoadCalls.length).toBe(0);
  });

  it("resolves false without loading for an empty family", async () => {
    const { entry, ready } = loadFontByFamily("");
    expect(entry).toBeUndefined();
    await expect(ready).resolves.toBe(false);
    expect(fontsLoadCalls.length).toBe(0);
  });
});
