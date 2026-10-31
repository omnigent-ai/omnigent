import { describe, expect, it } from "vitest";
import { fileTypeLabel } from "./fileTypeLabel";

describe("fileTypeLabel", () => {
  it.each([
    ["torus.stl", "file", "STL 3D model (.stl)"],
    ["photo.png", "file", "PNG image (.png)"],
    ["guide.pdf", "file", "PDF document (.pdf)"],
    ["app.ts", "file", "TypeScript file (.ts)"],
    ["data.xyz", "file", "File (.xyz)"],
    ["Makefile", "file", "File"],
    [".env", "file", "File"],
    ["models", "folder", "Folder"],
  ] as const)("labels %s", (name, kind, expected) => {
    expect(fileTypeLabel(name, kind)).toBe(expected);
  });
});
