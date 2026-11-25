// Vite config for the performance baseline builds.
//
//   cd web && pnpm exec vite build --config perf/vite.perf.config.ts            # minified (headline)
//   cd web && pnpm exec vite build --config perf/vite.perf.config.ts --mode nomin # readable names + sourcemaps (attribution)
//
// Wraps the app's own vite.config.ts so every production setting stays
// identical; only the output directory, minification and sourcemaps differ.
// `--mode nomin` flips minification off and emits sourcemaps so CPU profiles
// map back to web/src files. All outputs land under web/perf/dist* — never the
// tracked omnigent/server/static path.

import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, mergeConfig, type UserConfig } from "vite";
import baseConfig from "../vite.config";

const here = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(here, "..");

export default defineConfig((env) => {
  const resolved = typeof baseConfig === "function" ? baseConfig(env) : baseConfig;
  const nomin = env.mode === "nomin";
  const overrides: UserConfig = {
    resolve: {
      alias: {
        "@": path.resolve(webRoot, "src"),
      },
    },
    build: {
      outDir: path.resolve(here, nomin ? "dist-nomin" : "dist"),
      emptyOutDir: true,
      minify: !nomin,
      sourcemap: nomin,
      rollupOptions: {
        output: {
          manualChunks: resolved.build?.rollupOptions?.output?.manualChunks,
        },
      },
    },
  };
  return mergeConfig(resolved as UserConfig, overrides);
});
