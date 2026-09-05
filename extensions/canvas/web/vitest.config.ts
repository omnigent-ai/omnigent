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
      "@testing-library/jest-dom": resolve(
        repoRoot,
        "web/node_modules/@testing-library/jest-dom",
      ),
      "@testing-library/react": resolve(
        repoRoot,
        "web/node_modules/@testing-library/react",
      ),
      react: resolve(repoRoot, "web/node_modules/react"),
      "react-dom": resolve(repoRoot, "web/node_modules/react-dom"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: [resolve(__dirname, "src/test-setup.ts")],
    include: [resolve(__dirname, "src/**/*.{test,spec}.{ts,tsx}")],
  },
};
