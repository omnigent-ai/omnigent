import { describe, expect, it } from "vitest";

// core/ is what every platform shares; a platform's page, capture and overlay stay outside.
const sources = import.meta.glob(["./*.ts", "!./*.test.ts"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const PLATFORM_ONLY = ["@/lib/host", "react-dom"];

function imports(source: string): string[] {
  // `from "x"`, a bare `import "x"`, and `import("x")`.
  return [...source.matchAll(/(?:from\s+|import\s*\(?\s*)["']([^"']+)["']/g)].map((m) => m[1]!);
}

describe("design tools core", () => {
  it("has its modules", () => {
    expect(Object.keys(sources)).toEqual(
      expect.arrayContaining(["./designPlatform.ts", "./toolActions.ts", "./toolSession.ts"]),
    );
  });

  it.each(Object.entries(sources))("%s imports nothing platform-specific", (_file, source) => {
    for (const path of imports(source)) {
      expect(path.startsWith("../"), path).toBe(false);
      // Their subpaths too (`react-dom/client`, `@/lib/host/...`).
      const platformOnly = PLATFORM_ONLY.filter(
        (name) => path === name || path.startsWith(`${name}/`),
      );
      expect(platformOnly, path).toEqual([]);
    }
    expect(source).not.toMatch(/omnigentDesktop/);
    // The page's operations are typed: how one is done in a page is its platform's.
    expect(source).not.toMatch(/executeJavaScript|Script\(/);
  });
});
