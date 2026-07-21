// Build for the desktop update overlay island.
//
// Produces a tiny standalone page (update-overlay.html + hashed JS/CSS) that
// mounts the SHARED `UpdateBanner` component (see src/update-overlay.tsx). The
// output lands directly in the Electron shell package (`electron/overlay/`) so
// electron-builder ships it and the shell can load it in a corner window —
// making the update UI independent of the connected server's web-bundle
// version. Run via `bun run build:overlay` (or npm); the shell build depends on
// this output existing (see electron/package.json build.files + the
// electron-build workflow).
//
// Kept separate from the main app build (vite.config.ts) so it emits no PWA
// service worker / manifest and writes to the shell dir rather than dist/.

import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  // Assets are loaded from a file:// page in the shell, so reference them
  // relatively rather than from the server root.
  base: "./",
  // The overlay is a single self-contained card — it has no use for the web
  // app's public/ assets (PWA icons, favicon). Disable publicDir so Vite doesn't
  // copy ~150KB of orphan PWA images into electron/overlay/, which would then
  // be shipped by electron-builder's `build.files: overlay/**/*`.
  publicDir: false,
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    // Ship straight into the Electron package so electron-builder picks it up.
    outDir: path.resolve(__dirname, "./electron/overlay"),
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "./update-overlay.html"),
    },
  },
});
