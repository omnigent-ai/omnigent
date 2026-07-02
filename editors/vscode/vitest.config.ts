import { defineConfig } from "vitest/config";
import { resolve } from "path";

export default defineConfig({
  resolve: {
    alias: {
      // Stub the vscode module so unit tests run without an IDE host. The pure
      // logic under test never calls into vscode; only the thin adapters do, and
      // the controller test injects a fake panel via createWebviewPanel.
      vscode: resolve(__dirname, "src/test/__mocks__/vscode.ts"),
    },
  },
  test: {
    include: ["src/**/*.test.ts"],
    // Unit tests must not require the VS Code host or network.
    environment: "node",
  },
});
