import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { shikiManualChunk } from "./vite.shiki";

// Storybook needs the app's aliases and Tailwind pipeline, but not the
// production dev proxy, service-worker output, or SPA build destination.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: shikiManualChunk,
      },
    },
  },
});
