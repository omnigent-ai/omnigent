/**
 * Deterministic fingerprint of a build's **hashed Rollup outputs** (the
 * `assets/*` entry + lazy JS chunks and CSS — every file whose name carries a
 * content hash). It changes on every code/style deploy and is identical for
 * identical source on the same toolchain. `vite.config.ts`'s `emitPwaAssets`
 * plugin inlines it into `sw.js`, so the generated service worker's bytes
 * change per deploy — that is what makes the PWA update prompt fire.
 *
 * Intentionally OUT OF SCOPE: the static `index.html` template and `public/`
 * assets (icons, favicon, apple-touch-icon) are copied outside the Rollup graph
 * and are NOT fingerprinted. Any *code-driven* change already co-changes a
 * hashed chunk (which IS fingerprinted), so it moves the version. The one case
 * that does NOT is a hand-edit to the static template touching no TS module
 * (e.g. `<title>`, a `<meta>`, the manifest/icon links): no proactive update
 * prompt fires — but navigations always hit the network, so users still get the
 * new HTML on their next reload; they just aren't proactively nudged. A
 * dependency/Vite bump can change chunk hashing and harmlessly re-fire one
 * prompt. A non-cryptographic hash is fine here: it only needs to differ when
 * the build differs, not resist collisions.
 */
export function computeBuildVersion(fileNames: readonly string[]): string {
  // djb2 over the sorted filename list (sorted ⇒ order-independent).
  let hash = 5381;
  for (const name of [...fileNames].sort()) {
    for (let i = 0; i < name.length; i++) {
      hash = ((hash << 5) + hash + name.charCodeAt(i)) | 0;
    }
    hash = ((hash << 5) + hash + 10) | 0; // separator between names
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}
