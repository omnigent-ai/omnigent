import { describe, expect, it } from "vitest";

import { streamdownManualChunk } from "../../../vite.streamdown";

// The real dist layout, so the rule is checked against what streamdown ships:
// if an upgrade moves its lazy facades out of dist/, this stops matching and
// fails loudly instead of silently reverting to per-render facade fetches.
const distModules = Object.keys(import.meta.glob("/node_modules/streamdown/dist/*.js"));

describe("streamdownManualChunk", () => {
  it("sees streamdown's dist modules, including the render-time lazy facades", () => {
    expect(distModules.length).toBeGreaterThan(0);
    expect(
      distModules.some((p) => /\/mermaid-[^/]+\.js$/.test(p)),
      `mermaid facade missing from ${distModules.join(", ")}`,
    ).toBe(true);
    expect(
      distModules.some((p) => /\/highlighted-body-[^/]+\.js$/.test(p)),
      `highlighted-body facade missing from ${distModules.join(", ")}`,
    ).toBe(true);
  });

  it("groups every dist module - static chunk and facades - into one chunk", () => {
    for (const path of distModules) {
      expect(streamdownManualChunk(path), path).toBe("streamdown");
    }
  });

  it("matches the resolved module ids the bundler passes in", () => {
    expect(
      streamdownManualChunk(
        "/repo/web/node_modules/.pnpm/streamdown@1.0.0/node_modules/streamdown/dist/mermaid-ABC123.js",
      ),
    ).toBe("streamdown");
    expect(streamdownManualChunk("C:\\repo\\web\\node_modules\\streamdown\\dist\\index.js")).toBe(
      "streamdown",
    );
  });

  it("leaves other modules to the remaining chunking rules", () => {
    expect(
      streamdownManualChunk("/repo/web/node_modules/@streamdown/mermaid/dist/index.js"),
    ).toBeUndefined();
    expect(
      streamdownManualChunk("/repo/web/src/components/ai-elements/message.tsx"),
    ).toBeUndefined();
    expect(streamdownManualChunk("/repo/web/node_modules/shiki/dist/core.mjs")).toBeUndefined();
  });
});
