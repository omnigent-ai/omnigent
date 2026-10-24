import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const servicePort = Number(process.env.OMNIGENT_OFFICE_PORT ?? 6780);
const devPort = Number(process.env.OMNIGENT_OFFICE_DEV_PORT ?? 6781);

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: devPort,
    strictPort: true,
    proxy: { "/office/api": `http://127.0.0.1:${servicePort}` },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}", "server/**/*.test.ts", "shared/**/*.test.ts"],
  },
});
