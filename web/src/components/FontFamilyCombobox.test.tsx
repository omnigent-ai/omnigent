import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { FontFamilyCombobox } from "./FontFamilyCombobox";
import { FONT_CATALOG_BY_CATEGORY } from "@/lib/fontCatalog";
import { resetFontLoaderForTests } from "@/lib/webFontLoader";

// jsdom has no FontFaceSet; stub document.fonts.load so the loader (invoked when
// the dropdown opens to render previews) doesn't throw.
beforeEach(() => {
  Object.defineProperty(document, "fonts", {
    configurable: true,
    value: { load: vi.fn(() => Promise.resolve([])) },
  });
});

afterEach(() => {
  cleanup();
  // The loader dedups by resource across calls; clear it so each test's open
  // re-injects rather than hitting a cached promise.
  resetFontLoaderForTests();
  for (const node of document.querySelectorAll("[data-omnigent-font]")) node.remove();
  vi.restoreAllMocks();
});

function renderCombobox(props: Partial<Parameters<typeof FontFamilyCombobox>[0]> = {}) {
  const onChange = vi.fn();
  render(
    <FontFamilyCombobox
      category="sans"
      value=""
      onChange={onChange}
      defaultLabel="System default"
      ariaLabel="UI font family"
      testId="test-font"
      previewFallback="var(--font-sans)"
      {...props}
    />,
  );
  return { onChange };
}

describe("FontFamilyCombobox", () => {
  it("renders every font in its category (plus the default row)", () => {
    renderCombobox({ category: "sans" });
    fireEvent.click(screen.getByTestId("test-font-trigger"));

    expect(screen.getByTestId("test-font-option-default")).toBeInTheDocument();
    for (const entry of FONT_CATALOG_BY_CATEGORY.sans) {
      if (!entry.family) continue; // the empty "system default" entry is the default row
      expect(screen.getByTestId(`test-font-option-${entry.family}`)).toBeInTheDocument();
    }
    // A code-only font must NOT leak into the sans list.
    expect(screen.queryByTestId("test-font-option-JetBrains Mono")).toBeNull();
  });

  it("calls onChange with the selected catalog family and loads its webfont", () => {
    const { onChange } = renderCombobox({ category: "sans" });
    fireEvent.click(screen.getByTestId("test-font-trigger"));
    // Opening the dropdown eagerly kicks the loader for each catalog face so
    // its preview renders in-face — the google-css2 path injects a stylesheet.
    expect(document.querySelector(`link[data-omnigent-font]`)).not.toBeNull();

    fireEvent.click(screen.getByTestId("test-font-option-Inter"));
    expect(onChange).toHaveBeenCalledWith("Inter");
  });

  it("calls onChange with an empty string for the default row", () => {
    const { onChange } = renderCombobox({ category: "sans", value: "Inter" });
    fireEvent.click(screen.getByTestId("test-font-trigger"));
    fireEvent.click(screen.getByTestId("test-font-option-default"));
    expect(onChange).toHaveBeenCalledWith("");
  });

  it("surfaces a stored non-catalog family as a selectable custom row", () => {
    const { onChange } = renderCombobox({ category: "sans", value: "My Local Font" });
    expect(screen.getByTestId("test-font-trigger")).toHaveTextContent("My Local Font");

    fireEvent.click(screen.getByTestId("test-font-trigger"));
    const custom = screen.getByTestId("test-font-option-custom-current");
    expect(custom).toHaveTextContent("My Local Font");
    fireEvent.click(custom);
    expect(onChange).toHaveBeenCalledWith("My Local Font");
  });

  it("shows a cross-category catalog family as a custom row for this category", () => {
    // "Fira Code" is a code-catalog family, absent from the sans list. Stored in
    // the sans slot it must render as Custom — not be silently swallowed by a
    // cross-category catalog match — so the user can see and keep their entry.
    const { onChange } = renderCombobox({ category: "sans", value: "Fira Code" });
    expect(screen.getByTestId("test-font-trigger")).toHaveTextContent("Fira Code");

    fireEvent.click(screen.getByTestId("test-font-trigger"));
    // It is NOT offered as a normal sans option…
    expect(screen.queryByTestId("test-font-option-Fira Code")).toBeNull();
    // …it is the custom row instead.
    const custom = screen.getByTestId("test-font-option-custom-current");
    expect(custom).toHaveTextContent("Fira Code");
    expect(custom).toHaveTextContent("Custom");
    fireEvent.click(custom);
    expect(onChange).toHaveBeenCalledWith("Fira Code");
  });

  it("treats a same-category catalog family as a known option, not custom", () => {
    // The mirror of the cross-category case: a fixedWidth family stored in the
    // fixedWidth slot is a normal option, so no custom row appears for it.
    renderCombobox({ category: "fixedWidth", value: "IBM Plex Mono" });
    fireEvent.click(screen.getByTestId("test-font-trigger"));
    expect(screen.getByTestId("test-font-option-IBM Plex Mono")).toBeInTheDocument();
    expect(screen.queryByTestId("test-font-option-custom-current")).toBeNull();
  });

  it("applies a typed family the catalog doesn't list (free-text escape hatch)", () => {
    const { onChange } = renderCombobox({ category: "code" });
    fireEvent.click(screen.getByTestId("test-font-trigger"));
    fireEvent.change(screen.getByTestId("test-font-input"), { target: { value: "Menlo" } });
    fireEvent.click(screen.getByTestId("test-font-option-custom"));
    expect(onChange).toHaveBeenCalledWith("Menlo");
  });
});
