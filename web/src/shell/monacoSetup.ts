// Monaco wiring shared by every Monaco surface in the file viewer.
//
// Two jobs:
//   1. Force Monaco to load from the locally-bundled copy, never a CDN.
//      The app is served from the server's static dir and runs in
//      network-restricted deployments, so @monaco-editor/react's default
//      CDN AMD loader would fail; loader.config({ monaco }) points it at the
//      bundled ESM instance instead.
//   2. Drive tokenization and theming from Shiki (github-light/github-dark)
//      via @shikijs/monaco so editor colors match the read-only Shiki views
//      and chat code blocks. Monaco's own Monarch tokenizers and language
//      services are left unused.
//
// Importing this module is the trigger that sets MonacoEnvironment and points
// @monaco-editor/react at the bundled instance. It lives behind the lazy
// MonacoCodeEditor import so Monaco stays out of the initial bundle.

import { loader } from "@monaco-editor/react";
import { shikiToMonaco } from "@shikijs/monaco";
import { createHighlighter } from "shiki";
import type { BundledLanguage, BundledTheme, HighlighterGeneric } from "shiki";
// editor.api.js gives the typed `monaco` namespace (it ships editor.api.d.ts).
// The .js suffix is required: monaco's package `exports` map ("./*": "./*")
// maps subpaths literally, so the extensionless path won't resolve.
import * as monaco from "monaco-editor/esm/vs/editor/editor.api.js";
// Side-effect import: registers all editor *contributions* (find, folding,
// multi-cursor, context menu, the diff editor, …) onto the same core — both
// entries route through the shared editor.api2 module. editor.api alone has no
// contributions, so Cmd+F / the find action wouldn't exist. We deliberately
// avoid editor.main.js, which would additionally bundle Monaco's own language
// grammars + services (TS/JSON/CSS/HTML workers): Shiki does tokenization and
// we ship no language workers, so those are dead weight (and would error on a
// missing worker). This keeps grammars out while still giving real editor UX.
import "monaco-editor/esm/vs/editor/edcore.main.js";
import type { ResolvedThemeMode } from "@/components/theme/themeMode";

// Shiki theme ids registered into Monaco; identical to the read-only viewer.
const LIGHT_THEME = "github-light";
const DARK_THEME = "github-dark";

// Monaco reads this global to construct its workers. The global is declared by
// monaco's own types (editor.api.d.ts), so no augmentation is needed. Set once
// at import time, before any model or editor is created. The base worker
// handles diff computation and links; tokenization is Shiki's job.
//
// `new Worker(new URL(..., import.meta.url))` is the build-tool-agnostic worker
// idiom: both Vite (standalone) and the universe monolith's rspack emit the
// worker as a content-hashed asset served from their own CDN/output — no
// hardcoded path, no manual copy. This matches the monolith's convention for
// worker/wasm assets (see ruff/pdf workers in webapp/web). The worker is an ESM
// module (it lives under monaco's `esm/` tree), hence `type: "module"`.
self.MonacoEnvironment = {
  getWorker: () =>
    new Worker(new URL("monaco-editor/esm/vs/editor/editor.worker.js", import.meta.url), {
      type: "module",
    }),
};

// Point @monaco-editor/react at the bundled instance instead of its CDN loader.
loader.config({ monaco });

type ShikiHighlighter = HighlighterGeneric<BundledLanguage, BundledTheme>;

let readyPromise: Promise<ShikiHighlighter> | null = null;
// Per-language load promise, cached so concurrent ensureLanguage() calls for
// the same id share one load instead of double-registering. Keyed before any
// await; cleared on failure so a later call can retry.
const languageLoads = new Map<BundledLanguage, Promise<void>>();

/**
 * The Monaco editor namespace, re-exported so callers don't deep-import the
 * ESM path directly. Use for types (``MonacoModule["editor"]``) and the rare
 * imperative call a component needs.
 */
export { monaco };
export type MonacoModule = typeof monaco;

/**
 * Create the Shiki highlighter (github light + dark) once and register its
 * themes with Monaco. Idempotent — repeated calls return the same promise, so
 * every editor instance shares one highlighter.
 *
 * @returns The shared Shiki highlighter instance, ready to tokenize.
 */
export function ensureMonacoReady(): Promise<ShikiHighlighter> {
  if (!readyPromise) {
    readyPromise = createHighlighter({
      themes: [LIGHT_THEME, DARK_THEME],
      langs: [],
    })
      .then((hl) => {
        // Registers the github themes under their ids so the editor's `theme`
        // option and monaco.editor.setTheme resolve them.
        shikiToMonaco(hl, monaco);
        return hl;
      })
      .catch((err: unknown) => {
        // Don't cache the failure — let the next call retry (matches ensureLanguage).
        readyPromise = null;
        throw err;
      });
  }
  return readyPromise;
}

/**
 * Ensure Shiki can tokenize `lang` and Monaco knows about it.
 *
 * Loads the grammar into the shared highlighter, registers the language id
 * with Monaco, then re-runs shikiToMonaco so the new tokenizer attaches. A
 * no-op for "text" (Monaco's built-in plaintext). Concurrent calls for the
 * same id share one in-flight load (deduped via languageLoads); a failed load
 * is evicted so a later call retries.
 *
 * @param lang A Shiki bundled language id or "text", e.g. `"typescript"`.
 */
export function ensureLanguage(lang: BundledLanguage | "text"): Promise<void> {
  if (lang === "text") return Promise.resolve();
  let load = languageLoads.get(lang);
  if (!load) {
    load = (async () => {
      const hl = await ensureMonacoReady();
      await hl.loadLanguage(lang);
      monaco.languages.register({ id: lang });
      shikiToMonaco(hl, monaco);
    })().catch((err: unknown) => {
      languageLoads.delete(lang);
      throw err;
    });
    languageLoads.set(lang, load);
  }
  return load;
}

/**
 * Map a Shiki language id to the Monaco language id for the editor's
 * `language` option. Shiki's "text" maps to Monaco's built-in "plaintext".
 *
 * @param lang A Shiki bundled language id or "text", e.g. `"python"`.
 * @returns The Monaco language id, e.g. `"python"` or `"plaintext"`.
 */
export function monacoLanguageId(lang: BundledLanguage | "text"): string {
  return lang === "text" ? "plaintext" : lang;
}

/**
 * Map the app's resolved palette to the Monaco theme id to apply.
 *
 * @param resolved Concrete palette from `normalizeResolvedTheme`, e.g. `"dark"`.
 * @returns The registered Monaco theme id, e.g. `"github-dark"`.
 */
export function resolvedThemeToMonaco(resolved: ResolvedThemeMode): string {
  return resolved === "dark" ? DARK_THEME : LIGHT_THEME;
}
