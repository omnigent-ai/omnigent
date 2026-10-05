import { afterEach, describe, expect, it } from "vitest";
import {
  DESIGN_SYSTEM_RECENTS_MAX,
  designSystemInstruction,
  designSystemName,
  detectDesignSystemKind,
  parseDesignSystemPointer,
  readRecentDesignSystems,
  rememberDesignSystem,
  serializeDesignSystemPointer,
  type DesignSystemRef,
} from "./designSystem";

const ACME: DesignSystemRef = { path: "/Users/me/brand/acme", kind: "full", name: "Acme" };

afterEach(() => localStorage.clear());

describe("detectDesignSystemKind", () => {
  it.each([
    [["_ds_manifest.json", "SKILL.md", "README.md"], "full"],
    [["_ds_manifest.json"], "full"],
    [["SKILL.md", "notes.txt"], "skill"],
    [["kit.json"], null],
    [[], null],
  ] as const)("%j is %s", (names, kind) => {
    expect(detectDesignSystemKind(names)).toBe(kind);
  });
});

describe("designSystemName", () => {
  it("prefers the manifest namespace", () => {
    expect(
      designSystemName({
        folder: "/a/brand",
        manifest: '{"namespace":" Acme "}',
        skill: "---\nname: other\n---",
      }),
    ).toBe("Acme");
  });

  it("falls back to the SKILL.md frontmatter name", () => {
    expect(
      designSystemName({
        folder: "/a/brand",
        manifest: "{",
        skill: '---\ndescription: x\nname: "acme-slides"\n---\n# body',
      }),
    ).toBe("acme-slides");
  });

  it("falls back to the folder name and caps the length", () => {
    expect(designSystemName({ folder: "/a/brand/" })).toBe("brand");
    expect(designSystemName({ folder: "/a", manifest: `{"namespace":"${"x".repeat(200)}"}` })).toBe(
      "x".repeat(80),
    );
  });

  it("ignores a name outside the frontmatter", () => {
    expect(designSystemName({ folder: "/a/brand", skill: "# Title\nname: nope" })).toBe("brand");
  });
});

describe("parseDesignSystemPointer", () => {
  it("accepts absolute and relative paths", () => {
    expect(parseDesignSystemPointer(serializeDesignSystemPointer(ACME))).toEqual(ACME);
    expect(
      parseDesignSystemPointer('{"path":".omnigent/design-system","kind":"skill","name":"B"}'),
    ).toEqual({ path: ".omnigent/design-system", kind: "skill", name: "B" });
    expect(parseDesignSystemPointer('{"path":"C:\\\\brand","kind":"full","name":"W"}').path).toBe(
      "C:\\brand",
    );
  });

  it("strips a trailing slash and falls back to the folder name", () => {
    expect(parseDesignSystemPointer('{"path":"/x/brand/","kind":"full"}')).toEqual({
      path: "/x/brand",
      kind: "full",
      name: "brand",
    });
  });

  it.each([
    ["{", "design-system.json is not valid JSON"],
    ["[]", "design-system.json must be a JSON object"],
    ['{"kind":"full"}', 'design-system.json needs a "path"'],
    ['{"path":"","kind":"full"}', 'design-system.json needs a "path"'],
    ['{"path":"../up","kind":"full"}', '"path" must not contain "." or ".." segments'],
    ['{"path":"/a/../b","kind":"full"}', '"path" must not contain "." or ".." segments'],
    ['{"path":"/a","kind":"kit"}', '"kind" must be "full" or "skill"'],
  ])("rejects %s", (text, reason) => {
    expect(() => parseDesignSystemPointer(text)).toThrow(reason);
  });
});

describe("designSystemInstruction", () => {
  it("names the path and kind", () => {
    expect(designSystemInstruction(ACME)).toBe(
      "Follow the design system at `/Users/me/brand/acme` (`full`). Read its SKILL.md first.",
    );
  });
});

describe("recent design systems", () => {
  it("keeps recents per host, most recent first, deduped by path", () => {
    rememberDesignSystem("h1", ACME);
    rememberDesignSystem("h1", { path: "/b", kind: "skill", name: "B" });
    rememberDesignSystem("h1", { ...ACME, name: "Acme 2" });
    rememberDesignSystem("h2", { path: "/c", kind: "skill", name: "C" });
    expect(readRecentDesignSystems("h1")).toEqual([
      { ...ACME, name: "Acme 2" },
      { path: "/b", kind: "skill", name: "B" },
    ]);
    expect(readRecentDesignSystems("h2").map((r) => r.name)).toEqual(["C"]);
    expect(readRecentDesignSystems("h3")).toEqual([]);
  });

  it("caps the list", () => {
    for (let i = 0; i < DESIGN_SYSTEM_RECENTS_MAX + 2; i++) {
      rememberDesignSystem("h1", { path: `/s${i}`, kind: "full", name: `S${i}` });
    }
    const recents = readRecentDesignSystems("h1");
    expect(recents).toHaveLength(DESIGN_SYSTEM_RECENTS_MAX);
    expect(recents[0].name).toBe(`S${DESIGN_SYSTEM_RECENTS_MAX + 1}`);
  });

  it("ignores corrupt storage and invalid entries", () => {
    localStorage.setItem("omnigent.design.systems", "{");
    expect(readRecentDesignSystems("h1")).toEqual([]);
    localStorage.setItem(
      "omnigent.design.systems",
      JSON.stringify({ h1: [{ path: "/ok", kind: "full", name: "Ok" }, { path: 3 }, "x"] }),
    );
    expect(readRecentDesignSystems("h1")).toEqual([{ path: "/ok", kind: "full", name: "Ok" }]);
  });
});
