import { describe, expect, it, vi } from "vitest";
import type { HostFilesystemEntry } from "@/hooks/useHostFilesystem";
import { readFixtureFile } from "@/test/designSystemFixture";
import { DESIGN_SYSTEM_POINTER, parseDesignSystemPointer } from "./designSystem";
import {
  IMPORT_CONCURRENCY,
  importDesignSystem,
  listDesignSystemSource,
  planDesignSystemImport,
  type ImportPlan,
  type SourceFile,
} from "./designSystemImport";
import { DS_ASSET_MAX_BYTES, DS_DECK_MAX_BYTES } from "./designSystemInjection";

const SOURCE = "/Users/me/brand/acme";
const MB = 1024 * 1024;
const file = (path: string, bytes = 10): SourceFile => ({ path, bytes });

/** A host listing over a synthetic tree: folder path to entries. */
function hostTree(tree: Record<string, [string, "file" | "directory", number?][]>) {
  return vi.fn(async (dir: string) => {
    const entries = tree[dir];
    if (!entries) throw new Error(`404 ${dir}`);
    return {
      truncated: false,
      entries: entries.map(([name, type, bytes]): HostFilesystemEntry => ({
        name,
        path: `${dir}/${name}`,
        type,
        bytes: type === "file" ? (bytes ?? 10) : null,
        modified_at: 1,
      })),
    };
  });
}

describe("listDesignSystemSource", () => {
  it("lists the allowlisted files and folders and never walks the skip list", async () => {
    const list = hostTree({
      [SOURCE]: [
        ["SKILL.md", "file"],
        ["_ds_manifest.json", "file"],
        ["package.json", "file"],
        ["fonts", "directory"],
        ["assets", "directory"],
        ["uploads", "directory"],
        ["preview", "directory"],
        ["scripts", "directory"],
      ],
      [`${SOURCE}/fonts`]: [["brand.woff2", "file", 300]],
      [`${SOURCE}/assets`]: [["brand", "directory"]],
      [`${SOURCE}/assets/brand`]: [["logo.svg", "file", 40]],
    });
    const source = await listDesignSystemSource(SOURCE, list);
    expect(source.files).toEqual([
      file("SKILL.md"),
      file("_ds_manifest.json"),
      file("fonts/brand.woff2", 300),
      file("assets/brand/logo.svg", 40),
    ]);
    expect(source.folders).toEqual(["fonts", "assets", "uploads", "preview", "scripts"]);
    const listed = list.mock.calls.map(([dir]) => dir);
    expect(listed).not.toContain(`${SOURCE}/uploads`);
    expect(listed).not.toContain(`${SOURCE}/scripts`);
  });

  it("refuses a folder whose listing was cut short", async () => {
    const list = vi.fn(async () => ({ truncated: true, entries: [] }));
    await expect(listDesignSystemSource(SOURCE, list)).rejects.toThrow(
      `${SOURCE} has too many files to import`,
    );
  });
});

describe("planDesignSystemImport", () => {
  it("keeps allowlisted files with allowed extensions and reports the rest", () => {
    const plan = planDesignSystemImport({
      files: [
        file("SKILL.md"),
        file("README.md"),
        file("_ds_manifest.json"),
        file("colors_and_type.css"),
        file("notes.txt"),
        file("fonts/brand.woff2"),
        file("assets/logo.svg"),
        file("assets/run.js"),
        file("templates/title.html"),
        file("slides/intro.slides.html"),
        file("ui_kits/button.svg"),
      ],
      folders: ["fonts", "assets", "templates", "slides", "uploads", "ui_kits", "preview"],
    });
    expect(plan.files.map((f) => f.path)).toEqual([
      "README.md",
      "SKILL.md",
      "_ds_manifest.json",
      "assets/logo.svg",
      "colors_and_type.css",
      "fonts/brand.woff2",
      "slides/intro.slides.html",
      "templates/title.html",
    ]);
    expect(plan.skipped).toEqual([
      { path: "assets/run.js", reason: "not an allowed file type" },
      { path: "notes.txt", reason: "not part of a design system" },
      { path: "ui_kits/button.svg", reason: "not part of a design system" },
      { path: "preview/", reason: "never imported" },
      { path: "ui_kits/", reason: "never imported" },
      { path: "uploads/", reason: "never imported" },
    ]);
    expect(plan.totalBytes).toBe(80);
  });

  it("skips files over 2 MB and stops at the 20 MB total", () => {
    const big = Array.from({ length: 12 }, (_, i) =>
      file(`assets/${String(i).padStart(2, "0")}.png`, 2 * MB),
    );
    const plan = planDesignSystemImport({
      files: [file("SKILL.md"), file("fonts/huge.ttf", DS_ASSET_MAX_BYTES + 1), ...big],
      folders: [],
    });
    expect(plan.totalBytes).toBeLessThanOrEqual(DS_DECK_MAX_BYTES);
    expect(plan.files.map((f) => f.path)).toEqual([
      "SKILL.md",
      ...big.slice(0, 9).map((f) => f.path),
    ]);
    expect(plan.skipped).toEqual([
      ...big.slice(9).map((f) => ({ path: f.path, reason: "over the 20 MB total" })),
      { path: "fonts/huge.ttf", reason: "larger than 2 MB" },
    ]);
  });

  it("rejects names that could leave the import folder", () => {
    const plan = planDesignSystemImport({
      files: [file("assets/../SKILL.md"), file("assets/a\\b.svg")],
      folders: [],
    });
    expect(plan.files).toEqual([]);
    expect(plan.skipped.map((s) => s.reason)).toEqual(["unsupported name", "unsupported name"]);
  });
});

describe("importDesignSystem", () => {
  const ref = { path: SOURCE, kind: "full" as const, name: "Acme" };
  const plan: ImportPlan = {
    files: ["SKILL.md", "colors_and_type.css", "assets/logo.svg", "fonts/fixture-sans.woff2"].map(
      (p) => file(p),
    ),
    skipped: [],
    totalBytes: 40,
  };

  it("copies about four at a time with progress, then writes the pointer last", async () => {
    let active = 0;
    let peak = 0;
    const writes: [string, string][] = [];
    const progress: number[] = [];
    const result = await importDesignSystem(plan, ref, {
      read: async (rel) => {
        active += 1;
        peak = Math.max(peak, active);
        await new Promise((r) => {
          setTimeout(r, 5);
        });
        active -= 1;
        return readFixtureFile(rel)!;
      },
      write: async (path, _content, encoding) => {
        writes.push([path, encoding]);
      },
      onProgress: (done, total) => progress.push(done / total),
    });

    expect(result.errors).toEqual([]);
    expect(peak).toBe(IMPORT_CONCURRENCY);
    expect(progress).toEqual([0.25, 0.5, 0.75, 1]);
    expect(writes.slice(0, 4).sort()).toEqual([
      [".omnigent/design-system/SKILL.md", "utf-8"],
      [".omnigent/design-system/assets/logo.svg", "utf-8"],
      [".omnigent/design-system/colors_and_type.css", "utf-8"],
      [".omnigent/design-system/fonts/fixture-sans.woff2", "base64"],
    ]);
    expect(writes.at(-1)).toEqual([DESIGN_SYSTEM_POINTER, "utf-8"]);
    expect(result.ref).toEqual({
      path: ".omnigent/design-system",
      kind: "full",
      name: "Acme",
      importedFrom: SOURCE,
    });
  });

  it("reports per-file errors and never writes the pointer when any file fails", async () => {
    const write = vi.fn(async (path: string) => {
      if (path.endsWith("logo.svg")) throw new Error("507 Insufficient Storage");
    });
    const result = await importDesignSystem(plan, ref, {
      read: async (rel) =>
        rel === "SKILL.md"
          ? { encoding: "utf-8", content: "partial", bytes: 7, truncated: true }
          : readFixtureFile(rel)!,
      write,
      onProgress: () => {},
    });
    expect(result.ref).toBeNull();
    expect(result.errors).toEqual([
      { path: "SKILL.md", message: "too long to import (the read was truncated)" },
      { path: "assets/logo.svg", message: "507 Insufficient Storage" },
    ]);
    expect(write.mock.calls.map(([p]) => p)).not.toContain(DESIGN_SYSTEM_POINTER);
    expect(write.mock.calls.map(([p]) => p)).not.toContain(".omnigent/design-system/SKILL.md");
  });

  it("writes a pointer the viewer reads as an imported system", async () => {
    let pointer = "";
    await importDesignSystem({ files: [], skipped: [], totalBytes: 0 }, ref, {
      read: async () => null,
      write: async (path, content) => {
        if (path === DESIGN_SYSTEM_POINTER) pointer = content;
      },
      onProgress: () => {},
    });
    expect(parseDesignSystemPointer(pointer).importedFrom).toBe(SOURCE);
  });
});
