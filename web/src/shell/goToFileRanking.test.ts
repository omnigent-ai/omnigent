import { describe, expect, it } from "vitest";

import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

import { firstMatch, rankGoToFileResults, stillMatches } from "./goToFileRanking";

function file(path: string, type: WorkspaceFile["type"] = "file"): WorkspaceFile {
  return { path, name: path.split("/").pop() ?? path, type, bytes: null, modified_at: null };
}

function rankedPaths(paths: string[], query: string): string[] {
  return rankGoToFileResults(
    paths.map((path) => file(path)),
    query,
  ).map((row) => row.path);
}

describe("rankGoToFileResults", () => {
  it("orders by how the name matches: exact, prefix, word start, inside, folder, path", () => {
    expect(
      rankedPaths(
        [
          "docs/myparser/notes.txt", // only inside a folder name
          "lib/xparser.ts", // inside the name
          "src/json_parser.ts", // a word start in the name
          "parser/index.ts", // a folder starts with it
          "src/parserUtils.ts", // name prefix
          "a/b/parser", // exact name
        ],
        "parser",
      ),
    ).toEqual([
      "a/b/parser",
      "src/parserUtils.ts",
      "src/json_parser.ts",
      "lib/xparser.ts",
      "parser/index.ts",
      "docs/myparser/notes.txt",
    ]);
  });

  it("treats a lower-to-upper case change as a word start", () => {
    expect(rankedPaths(["src/xparser.ts", "src/jsonParser.ts"], "parser")).toEqual([
      "src/jsonParser.ts",
      "src/xparser.ts",
    ]);
    // A digit or punctuation before an uppercase letter isn't a case change.
    expect(rankedPaths(["1Parser.ts", "xParser.ts"], "parser")).toEqual([
      "xParser.ts",
      "1Parser.ts",
    ]);
    // Supplementary-plane letters are compared as whole characters, not surrogates.
    expect(rankedPaths(["a\u{10428}.ts", "x\u{10400}.ts"], "\u{10428}")).toEqual([
      "x\u{10400}.ts",
      "a\u{10428}.ts",
    ]);
  });

  it("keeps the lower tiers ahead of shorter or shallower paths", () => {
    // Inside the name beats a folder prefix, even when the path is longer and deeper.
    expect(rankedPaths(["parser/i.ts", "src/deep/longer/xparser.ts"], "parser")).toEqual([
      "src/deep/longer/xparser.ts",
      "parser/i.ts",
    ]);
    // A folder prefix beats a match elsewhere in the path, even when deeper.
    expect(rankedPaths(["x/myparser.d/n.ts", "parser/a/b/index.ts"], "parser")).toEqual([
      "parser/a/b/index.ts",
      "x/myparser.d/n.ts",
    ]);
  });

  it("matches case-insensitively", () => {
    expect(rankedPaths(["README.md", "docs/readme-old.md"], "readme")).toEqual([
      "README.md",
      "docs/readme-old.md",
    ]);
  });

  it("breaks ties by fewer segments, then shorter path, then code-unit order", () => {
    expect(rankedPaths(["b/a/app.ts", "z/app.ts", "a/app.ts", "a/apps.ts"], "app")).toEqual([
      "a/app.ts",
      "z/app.ts",
      "a/apps.ts",
      "b/a/app.ts",
    ]);
  });

  it("drops directories, which Go to File can't open", () => {
    const rows = rankGoToFileResults([file("src", "directory"), file("src/main.ts")], "src");
    expect(rows.map((row) => row.path)).toEqual(["src/main.ts"]);
  });

  it("splits name and folder, and marks the match in the name before the folder", () => {
    const [byName, byFolder] = rankGoToFileResults(
      [file("components/ui/button.tsx"), file("components/ui/index.ts")],
      "ui",
    );
    expect(byName).toMatchObject({ name: "index.ts", folder: "components/ui" });
    expect(byName.nameMatch).toBeNull();
    expect(byName.folderMatch).toEqual({ start: 11, end: 13 });
    expect(byFolder.name).toBe("button.tsx");

    const [inName] = rankGoToFileResults([file("src/Button.tsx")], "butt");
    expect(inName).toMatchObject({ name: "Button.tsx", folder: "src" });
    expect(inName.nameMatch).toEqual({ start: 0, end: 4 });
    expect(inName.folderMatch).toBeNull();
  });

  it("marks a query that spans the slash in both the folder and the name", () => {
    const [row] = rankGoToFileResults([file("src/App.tsx")], "src/App");
    expect(row).toMatchObject({ name: "App.tsx", folder: "src" });
    expect(row.folderMatch).toEqual({ start: 0, end: 3 });
    expect(row.nameMatch).toEqual({ start: 0, end: 3 });
  });

  it("marks only the folder when a spanning query ends at the slash", () => {
    const [row] = rankGoToFileResults([file("web/src/App.tsx")], "b/src/");
    expect(row.folderMatch).toEqual({ start: 2, end: 7 });
    expect(row.nameMatch).toBeNull();
  });

  it("keeps a match inside the name ahead of a spanning one", () => {
    const [row] = rankGoToFileResults([file("src/src.ts")], "src");
    expect(row.nameMatch).toEqual({ start: 0, end: 3 });
    expect(row.folderMatch).toBeNull();
  });

  it("is deterministic for a full 500-entry response", () => {
    const entries = Array.from({ length: 500 }, (_, index) =>
      file(`pkg${index % 7}/mod${(index * 37) % 500}/file${index}.ts`),
    );
    const once = rankGoToFileResults(entries, "file").map((row) => row.path);
    const reversed = rankGoToFileResults([...entries].reverse(), "file").map((row) => row.path);
    expect(once).toHaveLength(500);
    expect(reversed).toEqual(once);
  });
});

describe("case folding that changes length", () => {
  // "İ" lowercases to two UTF-16 units, so folded offsets aren't source offsets.
  it("highlights the right characters after an expanding character", () => {
    expect(firstMatch("İFILE.ts", "file")).toEqual({ start: 1, end: 5 });
    expect("İFILE.ts".slice(1, 5)).toBe("FILE");
  });

  it("highlights a hit on the last character", () => {
    expect(firstMatch("İndex.ts", "s")).toEqual({ start: 7, end: 8 });
    expect(firstMatch("aİ", "i̇")).toEqual({ start: 1, end: 2 });
  });

  it("ranks names with an expanding character without failing", () => {
    const rows = rankGoToFileResults([file("İndex.ts"), file("src/İFile.ts")], "s");
    expect(rows.map((row) => row.path)).toEqual(["İndex.ts", "src/İFile.ts"]);
    const [byWordStart] = rankGoToFileResults([file("İ_list.ts"), file("İxlist.ts")], "list");
    expect(byWordStart.path).toBe("İ_list.ts");
    expect(byWordStart.nameMatch).toEqual({ start: 2, end: 6 });
  });
});

describe("case folding that changes length, across the folder and name", () => {
  it("splits a spanning match after an expanding character in the folder", () => {
    const [row] = rankGoToFileResults([file("İx/App.ts")], "x/app");
    expect(row).toMatchObject({ name: "App.ts", folder: "İx" });
    expect(row.folderMatch).toEqual({ start: 1, end: 2 });
    expect(row.nameMatch).toEqual({ start: 0, end: 3 });
  });

  it("splits a spanning match with expanding characters on both sides of the slash", () => {
    const [row] = rankGoToFileResults([file("dİr/İndex.ts")], "R/İN");
    expect(row.folder.slice(row.folderMatch!.start, row.folderMatch!.end)).toBe("r");
    expect(row.name.slice(row.nameMatch!.start, row.nameMatch!.end)).toBe("İn");
  });
});

describe("context-dependent lowercasing", () => {
  // A word-final "Σ" lowercases to "ς" only when the whole string is lowercased,
  // as the server, the query and stillMatches do.
  it("matches and highlights a final sigma the way the query lowercases it", () => {
    expect(stillMatches("ΟΣ", "ΟΣ")).toBe(true);
    expect(firstMatch("ΟΣ", "ΟΣ")).toEqual({ start: 0, end: 2 });
    expect(firstMatch("ΟΣ_test.ts", "ΟΣ")).toEqual({ start: 0, end: 2 });
    expect(firstMatch("ΟΣ/index.ts", "ΟΣ")).toEqual({ start: 0, end: 2 });
  });

  it("ranks the exact final-sigma name ahead of a longer prefix match", () => {
    expect(rankedPaths(["οςlong", "ΟΣ"], "ΟΣ")).toEqual(["ΟΣ", "οςlong"]);
    const [exact] = rankGoToFileResults([file("src/ΟΣ")], "ΟΣ");
    expect(exact.nameMatch).toEqual({ start: 0, end: 2 });
  });
});

describe("firstMatch", () => {
  it("finds the first case-insensitive occurrence, or null", () => {
    expect(firstMatch("FooBarFoo", "foo")).toEqual({ start: 0, end: 3 });
    expect(firstMatch("naïve-café.md", "café")).toEqual({ start: 6, end: 10 });
    expect(firstMatch("main.ts", "xyz")).toBeNull();
    expect(firstMatch("main.ts", "  ")).toBeNull();
  });
});

describe("stillMatches", () => {
  it("keeps an earlier result only while it contains the text typed now", () => {
    expect(stillMatches("src/App.tsx", "app")).toBe(true);
    expect(stillMatches("src/App.tsx", "apps")).toBe(false);
    expect(stillMatches("src/App.tsx", "")).toBe(false);
  });
});
