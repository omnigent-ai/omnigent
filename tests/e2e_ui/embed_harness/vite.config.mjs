// Bundles the host harness AGAINST THE BUILT EMBED ARTIFACTS (web/dist-embed),
// resolving the island's bare `react` / `react-dom` / `react-router(-dom)`
// externals from web/node_modules — the same job the Databricks monolith's
// rspack does at its own build time. The test fixture copies this directory
// into `web/.embed-host-harness/` (so node_modules resolution walks up into
// web/) and builds from there.
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.resolve(here, "..");

// `react-router` is a transitive dep (of react-router-dom) that the embed
// island imports bare; pnpm's strict layout doesn't expose it under
// web/node_modules, so resolve it from react-router-dom's real (store)
// location, whose sibling node_modules carries it.
const rrdReal = fs.realpathSync(path.join(webDir, "node_modules", "react-router-dom"));
const requireFromRrd = createRequire(path.join(rrdReal, "package.json"));
const reactRouter = requireFromRrd.resolve("react-router");

export default defineConfig({
  root: here,
  base: "./",
  resolve: {
    alias: {
      "react-router": reactRouter,
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 10_000,
  },
});
