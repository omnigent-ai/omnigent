import { createRequire } from "node:module";
import { runInNewContext } from "node:vm";
import { build } from "vite";
import { beforeAll, expect, it } from "vitest";
import { forceMonacoBrowserPlatform } from "../../vite.embed.config";

let code: string;

beforeAll(async () => {
  const result = await build({
    configFile: false,
    logLevel: "silent",
    plugins: [forceMonacoBrowserPlatform()],
    build: {
      write: false,
      lib: {
        entry: createRequire(import.meta.url).resolve(
          "monaco-editor/esm/vs/base/common/platform.js",
        ),
        formats: ["iife"],
        name: "monacoPlatform",
      },
    },
  });
  if (!Array.isArray(result) || result[0].output[0].type !== "chunk") {
    throw new Error("Expected a bundled Monaco platform module");
  }
  code = result[0].output[0].code;
});

it.each([
  ["Macintosh", 2],
  ["Windows NT 10.0", 1],
  ["X11; Linux x86_64", 3],
])("uses the %s browser platform despite a host process shim", (userAgent, OS) => {
  const context = {
    navigator: { userAgent, language: "en-US" },
    // A host-provided process must never select native bindings in the web embed.
    process: { platform: "linux", versions: { node: "22.0.0" }, env: {} },
  };
  const platform = runInNewContext(`${code}; monacoPlatform`, context);
  expect({ OS: platform.OS, isWeb: platform.isWeb, isNative: platform.isNative }).toEqual({
    OS,
    isWeb: true,
    isNative: false,
  });
});
