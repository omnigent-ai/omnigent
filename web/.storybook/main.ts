import type { StorybookConfig } from "@storybook/react-vite";
import { mergeConfig } from "vite";

// `.ts` extension: Storybook loads this file with Node's own ESM resolver, which needs one.
import { shikiManualChunk } from "../vite.shiki.ts";

const config: StorybookConfig = {
  stories: ["../src/**/*.stories.@(js|jsx|mjs|ts|tsx)"],
  framework: {
    name: "@storybook/react-vite",
    options: {
      builder: {
        viteConfigPath: "vite.storybook.config.ts",
      },
    },
  },
  core: {
    disableTelemetry: true,
  },
  viteFinal: (viteConfig) =>
    mergeConfig(viteConfig, {
      build: {
        rollupOptions: {
          output: {
            manualChunks: shikiManualChunk,
          },
        },
      },
    }),
};

export default config;
