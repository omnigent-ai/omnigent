import { describe, expect, it } from "vitest";

import { mermaidFacadeModules } from "./eagerMermaidFacade";

// Raw text of the same files, to bound what the eager pin drags into the
// static bundle: the facade must stay a tiny re-export shim.
const rawFacades: Record<string, unknown> = import.meta.glob(
  "/node_modules/streamdown/dist/mermaid-*.js",
  { eager: true, query: "?raw", import: "default" },
);

describe("eagerMermaidFacade", () => {
  it("pins at least one facade module (a streamdown upgrade may move or rename it)", () => {
    expect(Object.keys(mermaidFacadeModules).length).toBeGreaterThan(0);
  });

  it("every pinned module re-exports streamdown's Mermaid component", () => {
    for (const [path, mod] of Object.entries(mermaidFacadeModules)) {
      const kind = typeof (mod as { Mermaid?: unknown }).Mermaid;
      expect(kind === "function" || kind === "object", `${path} must re-export Mermaid`).toBe(true);
    }
  });

  it("stays a tiny shim, so pinning it eagerly cannot bloat the entry chunk", () => {
    for (const [path, raw] of Object.entries(rawFacades)) {
      expect(typeof raw, path).toBe("string");
      expect((raw as string).length, `${path} should be a small re-export shim`).toBeLessThan(4096);
    }
  });
});
