// On-demand web font loader.
//
// Given a catalog entry (see lib/fontCatalog.ts), inject the stylesheet or
// `@font-face` rules that fetch its face data, then await readiness so callers
// know when glyphs are actually available (the moment to re-measure Monaco /
// refit xterm). Deduplicated on every axis:
//   - `bundled` fonts and empty families no-op (nothing to fetch).
//   - each stylesheet/asset is injected AT MOST ONCE, keyed by entry id.
//   - concurrent loads of the same font share one in-flight promise.
//
// SSR/no-DOM safe: with no `document`, load() resolves immediately (there's
// nothing to paint), so boot-time restore on the server is a harmless no-op.

import { type FontCatalogEntry, getFontByFamily } from "./fontCatalog";

// Marker so injected nodes are recognizable in the DOM (and idempotent across
// a hot reload that re-runs this module with a fresh Map).
const DATA_ATTR = "data-omnigent-font";

// entry.id → the load promise. Present = injection started; resolved = the
// font's `document.fonts.load()` settled (or timed out / errored — see below).
const loads = new Map<string, Promise<void>>();

/**
 * Await the browser actually having `family` ready to paint.
 *
 * `document.fonts.load('16px "Family"')` kicks the fetch for any matching
 * unloaded `@font-face` and resolves when they finish. A short timeout guards a
 * face that never resolves (offline, blocked CDN) so a caller awaiting readiness
 * isn't wedged — the CSS fallback stack renders meanwhile, and the glyphs swap
 * in if the fetch lands later (`font-display: swap`).
 */
async function awaitFontReady(family: string): Promise<void> {
  const fonts = document.fonts;
  if (!fonts?.load) return;
  const spec = `16px "${family}"`;
  const timeout = new Promise<void>((resolve) => window.setTimeout(resolve, 8000));
  try {
    await Promise.race([fonts.load(spec).then(() => undefined), timeout]);
  } catch {
    // A rejected load (e.g. malformed descriptor) must not reject the caller;
    // the fallback stack still renders.
  }
}

/** Inject a `<link rel="stylesheet">` for a Google CSS2 (or any CSS) href. */
function injectStylesheet(entry: FontCatalogEntry): void {
  if (!entry.cssUrl) return;
  // Guard against a duplicate node if this module's Map was reset (hot reload).
  if (document.querySelector(`link[${DATA_ATTR}="${entry.id}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = entry.cssUrl;
  link.setAttribute(DATA_ATTR, entry.id);
  document.head.appendChild(link);
}

/** Inject a `<style>` holding the entry's explicit `@font-face` rules. */
function injectFontFaces(entry: FontCatalogEntry): void {
  if (!entry.faces?.length) return;
  if (document.querySelector(`style[${DATA_ATTR}="${entry.id}"]`)) return;
  const css = entry.faces
    .map((face) => {
      const src = face.format ? `url(${face.url}) format('${face.format}')` : `url(${face.url})`;
      return [
        "@font-face {",
        `  font-family: '${entry.family}';`,
        `  font-style: ${face.style ?? "normal"};`,
        `  font-weight: ${face.weight ?? "400"};`,
        "  font-display: swap;",
        `  src: ${src};`,
        "}",
      ].join("\n");
    })
    .join("\n");
  const style = document.createElement("style");
  style.setAttribute(DATA_ATTR, entry.id);
  style.textContent = css;
  document.head.appendChild(style);
}

/**
 * Load the font for a catalog entry, returning a promise that resolves when its
 * glyphs are ready to paint. Deduplicated: repeat calls for the same entry share
 * one promise and inject the stylesheet/faces at most once. A `bundled` entry
 * (already in the app bundle) or an empty family resolves immediately.
 */
export function loadFont(entry: FontCatalogEntry): Promise<void> {
  if (typeof document === "undefined") return Promise.resolve();
  // Nothing to fetch: bundled faces are in the app CSS; an empty family is the
  // system default. Either way the browser already has it.
  if (entry.source === "bundled" || !entry.family) return Promise.resolve();

  const existing = loads.get(entry.id);
  if (existing) return existing;

  const promise = (async () => {
    if (entry.source === "google-css2") injectStylesheet(entry);
    else if (entry.source === "self-hosted") injectFontFaces(entry);
    await awaitFontReady(entry.family);
  })();
  loads.set(entry.id, promise);
  return promise;
}

/**
 * Load the font matching a typed/stored family NAME, if it's in the catalog.
 *
 * The bridge from the free-text font inputs: a name matching a catalog family
 * is loaded and its promise returned; a non-catalog name (a locally-installed
 * font, a partial name) resolves immediately and is left to the OS — the
 * existing free-text behavior, unchanged. Returns whether an entry matched so
 * callers can skip post-load work (Monaco re-measure) when nothing loaded.
 */
export function loadFontByFamily(family: string): {
  entry: FontCatalogEntry | undefined;
  ready: Promise<void>;
} {
  const entry = getFontByFamily(family);
  return { entry, ready: entry ? loadFont(entry) : Promise.resolve() };
}

/** Test-only: clear the in-flight/loaded dedup cache. */
export function resetFontLoaderForTests(): void {
  loads.clear();
}
