import { resolve } from "node:path";

const repoRoot = resolve(__dirname, "../../..");

export default {
  resolve: {
    alias: {
      "@omnigent/extension-sdk": resolve(
        repoRoot,
        "sdks/web-extension/src/index.ts",
      ),
      "@xyflow/react": resolve(repoRoot, "web/node_modules/@xyflow/react"),
      react: resolve(repoRoot, "web/node_modules/react"),
      "react-dom": resolve(repoRoot, "web/node_modules/react-dom"),
    },
  },
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    outDir: resolve(__dirname, "../src/omnigent_canvas/dist"),
    emptyOutDir: true,
    lib: {
      entry: resolve(__dirname, "src/main.tsx"),
      name: "OmnigentCanvas",
      formats: ["iife"],
      fileName: () => "extension.js",
    },
    rollupOptions: {
      output: {
        assetFileNames: (asset: { names?: string[] }) => {
          const name = asset.names?.[0] ?? "";
          return name.endsWith(".css") ? "extension.css" : "[name][extname]";
        },
      },
    },
  },
};
