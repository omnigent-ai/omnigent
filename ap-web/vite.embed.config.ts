// Intermediate library build for the embeddable Omnigent island.
//
// This is a PRE-BUNDLE step, not the final artifact. Vite bundles ap-web and
// ALL of its own dependencies (monaco, shiki, xterm, tiptap, react-query, …)
// into an ESM entry (`dist-embed/omnigent-embed.js`) plus a `chunks/` tree of
// code-split chunks (ap-web's natural `import()` boundaries are PRESERVED, so
// Monaco / shiki language grammars / mermaid diagrams stay lazy) and one scoped
// stylesheet (`dist-embed/omnigent-embed.css`). The universe monolith then
// ingests this graph into its OWN rspack graph (see
// `webapp/web/js/genai/omnigent/embed/loadOmnigentEmbed.ts` + the
// `@omnigent/embed` alias in `app.rsbuild.config.ts`) and emits the FINAL
// hashed/CDN chunks. So Vite owns ap-web's dependency resolution; rspack owns
// chunking, hashing, and serving.
//
// Only React/ReactDOM, react/jsx-runtime, and react-router(-dom) are left as
// BARE externals (not bundled, not shimmed). rspack resolves these bare
// specifiers to the host monolith's own copies (React 18 / react-router 6.4.1,
// matching ap-web), so there is a single React instance and a single
// react-router instance shared with the host — no `__OMNIGENT_SHARED__`, no MF.
// @tanstack/react-query is BUNDLED (the embed owns its own QueryClient now; see
// `embed.tsx`). Standalone (`main.tsx` / `vite.config.ts`) is unaffected.
//
// The CSS is post-processed to prefix every selector with `.omnigent-app` so
// Tailwind's preflight and base resets cannot leak out and clobber the host's
// chrome. `:root` / `html` / `body` are remapped onto the scope root itself.

import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import postcss from "postcss";
import { defineConfig, type Plugin } from "vite";

const SCOPE = ".omnigent-app";

/** Split a selector list on top-level commas (ignoring commas inside () or []). */
function splitTopLevel(selectorList: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let current = "";
  for (const ch of selectorList) {
    if (ch === "(" || ch === "[") depth++;
    else if (ch === ")" || ch === "]") depth = Math.max(0, depth - 1);
    if (ch === "," && depth === 0) {
      parts.push(current);
      current = "";
    } else {
      current += ch;
    }
  }
  if (current.trim() !== "") parts.push(current);
  return parts;
}

function prefixSelector(selector: string): string {
  const s = selector.trim();
  if (s === "" || s.startsWith(SCOPE)) return s;
  // Root-level selectors collapse onto the scope element itself.
  const rootMatch = s.match(/^(?::root|html|body)\b(.*)$/);
  if (rootMatch) {
    return `${SCOPE}${rootMatch[1]}`;
  }
  return `${SCOPE} ${s}`;
}

const scopePlugin = (): postcss.Plugin => ({
  postcssPlugin: "scope-omnigent",
  // Flatten `@layer` so embed rules become UNLAYERED. Tailwind v4 emits its
  // utilities inside `@layer utilities`, but the host monolith's base rules
  // (e.g. `body h2`) are unlayered — and in the cascade, unlayered styles ALWAYS
  // beat layered ones regardless of specificity. That let `body h2` override a
  // scoped utility like `.omnigent-app .text-[11px]`. Since every embed rule is
  // already scoped under `.omnigent-app`, we don't need layers for internal
  // ordering; dropping them makes the embed compete on specificity, which it
  // wins (`.omnigent-app .util` ≥ host's typical element selectors).
  AtRule(atRule) {
    if (atRule.name !== "layer") return;
    // Statement form `@layer a, b, c;` (no body) — just delete the declaration.
    if (!atRule.nodes) {
      atRule.remove();
      return;
    }
    // Block form `@layer name { … }` — hoist its children in place.
    atRule.replaceWith(atRule.nodes);
  },
  Rule(rule) {
    const parent = rule.parent;
    // Leave @keyframes step selectors (0%, from, to) untouched.
    if (parent && parent.type === "atrule" && /keyframes$/i.test((parent as postcss.AtRule).name)) {
      return;
    }
    if (rule.selectors.every((sel) => sel.trim().startsWith(SCOPE))) return;
    rule.selector = splitTopLevel(rule.selector).map(prefixSelector).join(", ");
  },
});
scopePlugin.postcss = true;

function scopeCss(css: string): string {
  return postcss([scopePlugin()]).process(css, { from: undefined }).css;
}

// Some of ap-web's bundled CJS deps (e.g. the `use-sync-external-store` shim
// pulled in transitively) do `var React = require("react")` at runtime. Because
// react/react-dom/react-router(-dom) are ESM EXTERNALS here, rolldown can't
// statically rewrite those CJS `require()` calls into the ESM imports — it
// routes them through its runtime `__require` helper, which in a browser (no
// global `require`) throws: `Calling \`require\` for "react" …`. (It also made
// rspack emit a `webpackEmptyContext` because the bare `require` token looks
// magic.)
//
// Fix: rewrite rolldown's `__require` helper so that, for our known externals,
// it returns the module that's ALREADY statically ESM-imported at the top of
// the bundle (rspack dedupes these to the host monolith's single React /
// react-router instance — same identity as the rest of the embed). We inject
// stable-named namespace imports and point the helper at them. Anything else
// still throws (no other external is `require()`d at runtime). This keeps the
// "single shared React" guarantee while making the CJS deps work in the browser.
function resolveExternalCjsRequire(externals: readonly string[]): Plugin {
  // Stable identifiers for the injected namespace imports (minifier-proof).
  const importName = (spec: string) => `__omnigentExt_${spec.replace(/[^a-zA-Z0-9]/g, "_")}`;

  // We anchor the patch on rolldown's own user-facing error STRING rather than
  // on the minifier-shaped `((x) => typeof require…` prefix. That string is
  // part of rolldown's documented behaviour (it links the user to the
  // bundling-cjs docs), so it's the most stable thing to key on — if it ever
  // changes, the locator below returns null, the per-build assertion fires, and
  // we get a loud, actionable failure instead of a silent runtime break.
  const HELPER_ERROR_MARKER = "Calling `require` for";
  // The helper assignment opens with `<var> = /* @__PURE__ */ ((<arg>) =>` and
  // the IIFE body closes with `})` after the throw. We locate the opener by
  // scanning back from the marker, and the closer by scanning forward.
  const HELPER_OPENER =
    /([A-Za-z_$][\w$]*)\s*=\s*\/\* @__PURE__ \*\/\s*\(\([A-Za-z_$][\w$]*\)\s*=>\s*typeof require[^]*$/;

  /**
   * Locate rolldown's `__require` helper assignment around the error marker.
   * Returns the [start, end) slice covering `<var> = …(function(){…throw…})`
   * plus the captured LHS variable name, or null if the shape is unrecognised.
   */
  function locateHelper(code: string): { start: number; end: number; varName: string } | null {
    const markerIdx = code.indexOf(HELPER_ERROR_MARKER);
    if (markerIdx === -1) return null;

    // Opener: the last `<var> = /* @__PURE__ */ ((x) => typeof require…` before
    // the marker. Search the prefix and take the final match.
    const opener = code.slice(0, markerIdx).match(HELPER_OPENER);
    if (!opener || opener.index === undefined) return null;
    const start = opener.index;
    const varName = opener[1];

    // Closer: the IIFE ends at the first `})` after the throw's marker.
    const closeIdx = code.indexOf("})", markerIdx);
    if (closeIdx === -1) return null;
    const end = closeIdx + 2;

    return { start, end, varName };
  }

  // rolldown emits the helper exactly once — in the entry (single-file build) or
  // in a shared runtime chunk (multi-chunk build). `renderChunk` runs per-chunk,
  // so most chunks won't contain it; we patch the one that does and assert at
  // the end that we patched exactly once.
  let patchedCount = 0;
  return {
    name: "resolve-external-cjs-require",
    enforce: "post",
    buildStart() {
      patchedCount = 0;
    },
    renderChunk(code) {
      const helper = locateHelper(code);
      if (!helper) return null;
      patchedCount++;

      const imports = externals
        .map((spec) => `import * as ${importName(spec)} from ${JSON.stringify(spec)};`)
        .join("\n");
      const table = externals
        .map((spec) => `${JSON.stringify(spec)}: ${importName(spec)}`)
        .join(", ");

      // Replace the helper with a function that resolves our externals from the
      // injected ESM namespaces (rspack dedupes those to the host's single React
      // / react-router). No `require` token survives, so rspack never sees a
      // magic require (no `webpackEmptyContext`); anything unexpected still
      // throws the same error rolldown would have.
      const replacement =
        `${helper.varName} = function(id) {\n` +
        `\tconst __omnigentExternals = { ${table} };\n` +
        `\tif (Object.prototype.hasOwnProperty.call(__omnigentExternals, id)) return __omnigentExternals[id];\n` +
        `\tthrow Error("Calling \`require\` for \\"" + id + "\\" in an environment that doesn't expose the \`require\` function.");\n` +
        `}`;

      const next =
        imports + "\n" + code.slice(0, helper.start) + replacement + code.slice(helper.end);
      return { code: next, map: null };
    },
    generateBundle() {
      if (patchedCount !== 1) {
        throw new Error(
          `resolve-external-cjs-require: expected to patch rolldown's __require helper exactly once, but patched it ${patchedCount} times. ` +
            `The runtime shim shape or chunking changed and the external CJS require fix would silently break. ` +
            `Re-check locateHelper() against the current rolldown output.`,
        );
      }
    },
  };
}

function scopeOmnigentCss(): Plugin {
  return {
    name: "scope-omnigent-css",
    enforce: "post",
    generateBundle(_options, bundle) {
      for (const file of Object.values(bundle)) {
        if (file.type === "asset" && file.fileName.endsWith(".css")) {
          const css =
            typeof file.source === "string"
              ? file.source
              : Buffer.from(file.source).toString("utf8");
          file.source = scopeCss(css);
        }
      }
    },
  };
}

// Bare externals: ap-web's React plumbing is NOT bundled. The host monolith's
// rspack resolves these specifiers to its own copies, so the embed shares the
// host's single React + react-router instance. Kept as bare strings (no shims,
// no globals) — rspack does the resolution at its build time.
const SHARED_EXTERNALS = [
  "react",
  "react-dom",
  "react/jsx-runtime",
  "react-router",
  "react-router-dom",
];

export default defineConfig({
  // `base: "./"` makes Vite reference the emitted Monaco worker via a RELATIVE
  // `new URL("./<worker>", import.meta.url)` (instead of an absolute
  // `/assets/...` string). Monaco is omnigent_ui's own dep, so Vite builds the
  // worker; the relative `new URL` then survives into the intermediate as a
  // module reference the monolith's rspack can resolve, re-emit, and
  // content-hash on its own CDN (see the `@omnigent/embed` wiring in
  // app.rsbuild.config.ts). rspack owns the FINAL hashed worker name.
  base: "./",
  plugins: [
    react(),
    tailwindcss(),
    scopeOmnigentCss(),
    resolveExternalCjsRequire(SHARED_EXTERNALS),
  ],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  // React reads process.env.NODE_ENV; replace at build time so the bundle
  // runs in a host without a `process` global.
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    outDir: path.resolve(__dirname, "./dist-embed"),
    emptyOutDir: true,
    cssCodeSplit: false,
    sourcemap: false,
    lib: {
      entry: path.resolve(__dirname, "./src/embed.tsx"),
      formats: ["es"],
    },
    rollupOptions: {
      // Bare externals — see SHARED_EXTERNALS. rspack resolves them to the
      // host's React/react-router; everything else is bundled by Vite here.
      external: SHARED_EXTERNALS,
      output: {
        // PRESERVE Vite's natural code-splitting: ap-web already lazy-loads its
        // heaviest deps (`lazy(() => import("./MonacoCodeEditor"))`, shiki
        // language grammars, mermaid diagrams …). We DON'T inline those — Vite
        // emits the entry plus a `chunks/` tree of dynamic-import chunks, and the
        // monolith's rspack follows those `./chunks/*` imports into ITS OWN graph
        // and re-chunks/hashes/CDN-serves them (the entry is the only eager load;
        // Monaco etc. stay lazy). Stable, unhashed names here — rspack does the
        // FINAL content-hashing downstream. The Monaco worker + wasm assets are
        // emitted under `assets/` and re-emitted by rspack.
        entryFileNames: "omnigent-embed.js",
        chunkFileNames: "chunks/[name]-[hash].js",
        assetFileNames: (assetInfo) => {
          const name = assetInfo.names?.[0] ?? "";
          return name.endsWith(".css") ? "omnigent-embed.css" : "assets/[name].[ext]";
        },
      },
    },
  },
});
