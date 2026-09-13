// Build for the desktop server selector v2 (the gated "connect to a server"
// screen, an alternative to the hand-written electron/setup/index.html).
//
// Produces a self-contained page (server-selector-v2.html + hashed JS/CSS) that
// mounts LoginPageV2 wired to the shell's `omnigentSetup` preload bridge. The
// output lands directly in the Electron shell package (`electron/server-selector-v2/`)
// so electron-builder ships it and the shell can load it from a file:// window
// when OMNIGENT_SERVER_SELECTOR_V2=1. Run via `bun run build:server-selector-v2`.
//
// Mirrors vite.update-overlay.config.ts: relative base for file:// loading, no
// publicDir (the page needs none of the web app's PWA assets), same `@` alias.

import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  // Loaded from a file:// page in the shell, so reference assets relatively.
  base: "./",
  // Fixed port so the Electron shell's dev loader can default to this URL
  // (see serverSelectorV2DevUrl in electron/src/main.js) without an env var.
  // strictPort: fail loudly rather than drift to another port the shell can't find.
  server: { port: 5174, strictPort: true },
  // No use for the web app's public/ assets (PWA icons, favicon) — don't copy them
  // into electron/server-selector-v2/ where electron-builder would then ship them.
  publicDir: false,
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    // Ship straight into the Electron package so electron-builder picks it up.
    outDir: path.resolve(__dirname, "./electron/server-selector-v2"),
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "./server-selector-v2.html"),
    },
  },
});
