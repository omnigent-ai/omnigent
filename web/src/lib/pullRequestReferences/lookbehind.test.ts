import { expect, it } from "vitest";

// The build targets Safari 15, which can't parse a lookbehind: one would fail as the module loads.
const sources = import.meta.glob(["./*.ts", "!./*.test.ts"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

it("builds its patterns without lookbehind, which Safari before 16.4 can't parse", () => {
  expect(Object.keys(sources)).toEqual(expect.arrayContaining(["./detect.ts", "./mentions.ts"]));
  for (const [file, source] of Object.entries(sources)) {
    expect(source, file).not.toMatch(/\(\?<[=!]/);
  }
});
