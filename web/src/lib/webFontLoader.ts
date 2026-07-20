// On-demand web font loader.
//
// Given a catalog entry (see lib/fontCatalog.ts), inject the stylesheet or
// `@font-face` rules that fetch its face data, then await readiness so callers
// know when glyphs are actually available (the moment to re-measure Monaco /
// refit xterm). Deduplicated on every axis:
//   - `bundled` fonts and empty families no-op (nothing to fetch).
//   - each stylesheet/asset is injected AT MOST ONCE, keyed by its resource URL
//     (NOT the catalog id) so two entries sharing one resource dedupe.
//   - concurrent loads of the same resource share one in-flight promise.
//
// Readiness is signalled ONLY by genuine `document.fonts.load` settlement — no
// timer resolves it. `document.fonts.load` is itself bounded (it settles when
// the browser's font fetch finishes or fails, per spec — it doesn't hang), and
// our two consumers never block on it: the UI font path fires-and-forgets (CSS
// `font-display: swap` paints the swap), and the code font path chains `.then`
// to re-emit only on a `true` result. So a wall-clock timeout would either fire
// a premature remeasure against the fallback cell (if reported as ready) or
// suppress the real late notification — the exact bugs it was meant to avoid.
// We deliberately omit it.
//
// SSR/no-DOM safe: with no `document`, load() resolves immediately (there's
// nothing to paint), so boot-time restore on the server is a harmless no-op.

import { type FontCatalogEntry, fontLoadKey, getFontByFamily } from "./fontCatalog";

// Marker + load-key attribute on injected nodes: recognizable in the DOM and
// idempotent across a hot reload that re-runs this module with a fresh Map. The
// key is the resource URL (see fontLoadKey), so a shared resource maps to one
// node regardless of which catalog entry triggered it.
const DATA_ATTR = "data-omnigent-font";

// load key → the load promise. Present = injection started; resolved(true) =
// the font's glyphs are genuinely ready; resolved(false) = the load failed (bad
// CDN, blocked). Keyed by resource identity so identical resources share one
// load. The promise resolves ONLY on genuine `document.fonts.load` settlement —
// never on a timer — so a consumer's remeasure/refit fires exactly when (and
// only if) the glyphs actually arrive, however late, and never against the
// fallback cell.
const loads = new Map<string, Promise<boolean>>();

// load key → the node(s) this loader injected for it. Tracked in a Map rather
// than found via a DOM attribute selector: the key is a URL (with `:?&;@`) and a
// quoted attribute selector over such a value is unreliable across engines. The
// `DATA_ATTR` on each node stays only as a debugging marker.
const injected = new Map<string, Element[]>();

/**
 * Remove every node this loader injected for `key` (the `<link>` and/or the
 * `@font-face` `<style>`). Called on a retryable failure so a later attempt
 * genuinely reinjects and re-fetches — and, for self-hosted faces, so the
 * errored CSS-connected `FontFace` is dropped from `document.fonts` (a fresh
 * `<style>` mints a fresh face rather than `document.fonts.load` returning the
 * cached rejected one forever).
 */
function removeInjected(key: string): void {
  for (const node of injected.get(key) ?? []) node.remove();
  injected.delete(key);
}

/**
 * Inject a `<link rel="stylesheet">` for a Google CSS2 (or any CSS) href and
 * resolve once the browser has parsed it (`load` event) — so the @font-face
 * rules are registered before we ask `document.fonts` about them. Rejects on the
 * link's `error` event (removing its own node). If a node for this key already
 * exists (hot reload / a sibling entry sharing the URL), resolves immediately.
 */
function injectStylesheet(key: string, cssUrl: string): Promise<void> {
  if (injected.has(key)) return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = cssUrl;
    link.setAttribute(DATA_ATTR, key);
    link.addEventListener("load", () => resolve(), { once: true });
    link.addEventListener(
      "error",
      () => {
        // Drop the failed node so a later retry (the promise is evicted on
        // failure) genuinely re-injects instead of the has-guard skipping it.
        removeInjected(key);
        reject(new Error(`stylesheet failed: ${cssUrl}`));
      },
      { once: true },
    );
    document.head.appendChild(link);
    injected.set(key, [link]);
  });
}

/** Inject a `<style>` holding an entry's explicit `@font-face` rules (once). */
function injectFontFaces(key: string, entry: FontCatalogEntry): void {
  if (!entry.faces?.length) return;
  if (injected.has(key)) return;
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
  style.setAttribute(DATA_ATTR, key);
  style.textContent = css;
  document.head.appendChild(style);
  injected.set(key, [style]);
}

/**
 * Outcome of a readiness probe:
 *   - `ready`       — glyphs genuinely available; remeasure now.
 *   - `failed`      — a real asset failure (rejected load, or empty/not-ready
 *     result meaning the rules didn't take). RETRYABLE: the caller drops the
 *     injected node so a later attempt re-fetches.
 *   - `unsupported` — no `document.fonts` API (old browser / jsdom). NOT a load
 *     failure, so NOT retryable — reinjecting would just thrash.
 */
type ReadyOutcome = "ready" | "failed" | "unsupported";

/**
 * Probe whether the browser genuinely has `family` ready to paint.
 *
 * `document.fonts.load('16px "Family"')` kicks the fetch for matching unloaded
 * `@font-face`s and resolves with the FontFace objects it matched. An EMPTY
 * match array (or a rejected load) is `failed` — an empty result means the rules
 * weren't registered / the face errored, so reporting success would fire a
 * premature remeasure against the fallback cell. A missing API is `unsupported`,
 * kept distinct so the caller doesn't treat it as a retryable asset failure.
 * Never rejects and never resolves on a timer, so a consumer chained off it
 * remeasures exactly when the glyphs actually arrive, however late.
 */
async function fontsReady(family: string): Promise<ReadyOutcome> {
  const fonts = document.fonts;
  if (!fonts?.load) return "unsupported";
  try {
    const faces = await fonts.load(`16px "${family}"`);
    const ok = Array.isArray(faces) ? faces.length > 0 : Boolean(faces);
    return ok ? "ready" : "failed";
  } catch {
    return "failed";
  }
}

/**
 * Load the font for a catalog entry, returning a promise that resolves `true`
 * when its glyphs are genuinely ready to paint (and `false` if the load fails).
 * Deduplicated by RESOURCE identity (fontLoadKey), so two entries sharing one
 * stylesheet/asset inject once and share one in-flight promise. A `bundled`
 * entry (already in the app bundle) or an empty family resolves immediately.
 *
 * Resolution tracks genuine `document.fonts` settlement, never a timer: a slow
 * font resolves whenever it truly arrives (so the remeasure/refit lands then,
 * not before), and a fast font leaves no dangling timer. Callers that must not
 * block on a stalled CDN should not `await` this directly — the UI font path
 * fires-and-forgets (CSS `font-display: swap` paints the swap), and the code
 * font path chains a `.then` that re-emits so widgets remeasure on genuine
 * arrival.
 */
export function loadFont(entry: FontCatalogEntry): Promise<boolean> {
  if (typeof document === "undefined") return Promise.resolve(false);
  // Nothing to fetch: bundled faces are in the app CSS; an empty family is the
  // system default. Either way the browser already has it — treat as ready.
  if (entry.source === "bundled" || !entry.family) return Promise.resolve(true);

  const key = fontLoadKey(entry);
  const existing = loads.get(key);
  if (existing) return existing;

  // The inner load yields a ReadyOutcome; the outer `.then` maps it to the
  // cached boolean AND handles retry eviction (where `promise` is in scope).
  const outcome = (async (): Promise<ReadyOutcome> => {
    if (entry.source === "google-css2" && entry.cssUrl) {
      // Wait for the stylesheet to parse so its @font-face rules are registered
      // BEFORE we query document.fonts — otherwise fonts.load resolves empty and
      // we'd report a premature ready with no later notification. The link's own
      // error handler removes its node, so a rejection here is a retryable
      // failure with nothing left to reinject.
      try {
        await injectStylesheet(key, entry.cssUrl);
      } catch {
        return "failed";
      }
    } else if (entry.source === "self-hosted") {
      injectFontFaces(key, entry);
    }
    return fontsReady(entry.family);
  })();

  const promise = outcome.then((result) => {
    if (result === "failed") {
      // A real asset failure: drop THIS attempt's injected node(s) so a retry
      // reinjects and re-fetches (and, for self-hosted, mints a fresh FontFace
      // instead of reusing the errored one), then evict so the next call re-runs.
      // Guard against clobbering a newer in-flight load for the same key.
      if (loads.get(key) === promise) {
        removeInjected(key);
        loads.delete(key);
      }
    } else if (result === "unsupported") {
      // No document.fonts API (old browser / jsdom): NOT a load failure. Leave
      // the injected node in place (CSS may still paint via font-display) and
      // evict without node removal so we don't thrash reinjecting on retry.
      if (loads.get(key) === promise) loads.delete(key);
    }
    return result === "ready";
  });
  loads.set(key, promise);
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
 * An optional `category` disambiguates a family offered in more than one role.
 */
export function loadFontByFamily(
  family: string,
  category?: Parameters<typeof getFontByFamily>[1],
): {
  entry: FontCatalogEntry | undefined;
  /** Resolves `true` when the matched font's glyphs genuinely arrive. */
  ready: Promise<boolean>;
} {
  const entry = getFontByFamily(family, category);
  return { entry, ready: entry ? loadFont(entry) : Promise.resolve(false) };
}

/** Test-only: clear the in-flight/loaded dedup cache and injected-node index. */
export function resetFontLoaderForTests(): void {
  loads.clear();
  injected.clear();
}
