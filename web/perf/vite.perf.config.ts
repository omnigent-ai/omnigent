// Perf builds reuse the app config but keep output separate from the tracked static files.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, mergeConfig, type UserConfig } from "vite";
import baseConfig from "../vite.config";

const here = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig((env) => {
  const resolved = typeof baseConfig === "function" ? baseConfig(env) : baseConfig;
  const nomin = env.mode === "nomin";
  return mergeConfig(resolved as UserConfig, {
    build: {
      outDir: path.resolve(here, nomin ? "dist-nomin" : "dist"),
      emptyOutDir: true,
      minify: !nomin,
      sourcemap: nomin,
    },
  });
});
