import { FlaskConical } from "lucide-react";
import { describe, expect, it } from "vitest";
import {
  LUCIDE_ICON_NAMES,
  lucideIcon,
  lucideIconLabel,
  searchLucideIcons,
} from "./lucideIconCatalog";

describe("lucideIconCatalog", () => {
  it("looks icons up by their lucide-react component name", () => {
    expect(lucideIcon("FlaskConical")).toBe(FlaskConical);
    expect(lucideIcon("NotALucideIcon")).toBeUndefined();
    // Object prototype keys are not icons.
    expect(lucideIcon("toString")).toBeUndefined();
    expect(LUCIDE_ICON_NAMES.length).toBeGreaterThan(1000);
  });

  it("labels names as lowercase words", () => {
    expect(lucideIconLabel("FlaskConical")).toBe("flask conical");
    expect(lucideIconLabel("ArrowDown01")).toBe("arrow down 01");
    expect(lucideIconLabel("ALargeSmall")).toBe("a large small");
  });

  it("matches names ignoring spaces, case and hyphens, prefix matches first", () => {
    expect(searchLucideIcons("")).toEqual(LUCIDE_ICON_NAMES);
    expect(searchLucideIcons("  Flask-Con ")[0]).toBe("FlaskConical");
    const books = searchLucideIcons("book");
    expect(books[0]).toBe("Book");
    expect(books).toContain("Notebook");
    expect(books.indexOf("Notebook")).toBeGreaterThan(books.indexOf("BookOpen"));
    expect(searchLucideIcons("zzzz-no-such-icon")).toEqual([]);
  });
});
