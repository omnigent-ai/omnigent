import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useTheme } from "next-themes";
import { afterEach, describe, expect, it } from "vitest";

import { ThemeProvider } from "@/components/theme/ThemeProvider";

import { FileTypeIcon, treeFileIcon } from "./FileTypeIcon";

function ThemeSwitch() {
  const { setTheme } = useTheme();
  return (
    <>
      <button type="button" onClick={() => setTheme("light")}>
        Light
      </button>
      <button type="button" onClick={() => setTheme("dark")}>
        Dark
      </button>
    </>
  );
}

function renderIcon(path: string) {
  return render(
    <ThemeProvider>
      <ThemeSwitch />
      <FileTypeIcon path={path} />
    </ThemeProvider>,
  );
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  document.querySelector("[data-icon-sprite]")?.remove();
});

describe("FileTypeIcon", () => {
  it("shows the tree's colored glyph for a language it knows, from one shared sprite", () => {
    const { container } = renderIcon("scripts/test_distribution.py");
    const icon = container.querySelector("svg[data-file-type-icon]");
    expect(icon?.getAttribute("data-file-type-icon")).toBe("file-tree-builtin-python");
    expect(icon?.querySelector("use")?.getAttribute("href")).toBe("#file-tree-builtin-python");
    expect((icon as SVGElement).style.color).toContain("light-dark(");
    expect(document.querySelectorAll("[data-icon-sprite]")).toHaveLength(1);
  });

  it("falls back to the themed group glyph where the tree has only its gray default", () => {
    expect(treeFileIcon("src/Main.java")).toBeNull();
    const { container } = renderIcon("src/Main.java");
    expect(container.querySelector("svg[data-file-type-icon]")).toBeNull();
    expect(container.querySelector("[data-file-type]")?.getAttribute("data-file-type")).toBe(
      "code",
    );
  });

  it("follows a live theme switch, since its colors resolve against its own color-scheme", async () => {
    const { container } = renderIcon("README.md");
    const icon = () => container.querySelector<SVGElement>("svg[data-file-type-icon]")!;
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Dark" })));
    expect(icon().style.colorScheme).toBe("dark");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Light" })));
    expect(icon().style.colorScheme).toBe("light");
  });
});
