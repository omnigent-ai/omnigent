// Test-only build of the e2e embed-host page (e2e-embed-host/, driven by
// tests/e2e_ui/embed): the embeddable `OmnigentApp` plus a minimal host page,
// with React and react-router bundled (unlike vite.embed.config.ts). Output is gitignored.
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  // The host page needs none of the web app's public/ assets (PWA icons).
  publicDir: false,
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: path.resolve(__dirname, "./dist-e2e-embed-host"),
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "./e2e-embed-host/index.html"),
    },
  },
});
