import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { FILE_ICON_PALETTE, FILE_ICON_TOKEN_COLORS } from "./fileTypeIconPalette";

// The vendored tree's stylesheet, where the trees define their icon colors.
const TREE_STYLE = readFileSync(resolve(__dirname, "vendored/file-tree.css"), "utf8");

function styledTokenColors(): Record<string, string> {
  const pattern =
    /\[data-file-tree-colored-icons\] \[data-icon-token="([a-z0-9-]+)"\] \{\s*color: var\(--trees-icon-([a-z]+)\);/g;
  return Object.fromEntries(
    [...TREE_STYLE.matchAll(pattern)].map(([, token, color]) => [token, color]),
  );
}

function styledPalette(): Record<string, string> {
  const pattern = /--trees-icon-([a-z]+): (light-dark\(#[0-9a-fA-F]+, #[0-9a-fA-F]+\))/g;
  return Object.fromEntries(
    [...TREE_STYLE.matchAll(pattern)].map(([, name, value]) => [name, value]),
  );
}

describe("the copied file tree icon palette", () => {
  it("gives every token the color the tree's stylesheet gives it", () => {
    const styled = styledTokenColors();
    expect(Object.keys(styled).length).toBeGreaterThan(40);
    expect(FILE_ICON_TOKEN_COLORS).toEqual(styled);
  });

  it("uses the stylesheet's light and dark value for each color", () => {
    const styled = styledPalette();
    for (const [name, value] of Object.entries(FILE_ICON_PALETTE)) {
      expect(value, name).toBe(styled[name]);
    }
  });
});
