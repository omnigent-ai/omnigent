import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CODE_FONT_FAMILY_DEFAULT,
  CODE_FONT_FAMILY_FALLBACK,
  CODE_FONT_SIZE_DEFAULT,
  CODE_FONT_SIZE_MAX,
  CODE_FONT_SIZE_MIN,
  codeFontFamilyForEditor,
  readCodeFont,
  readCodeFontFamily,
  readCodeFontSizePx,
  subscribeCodeFont,
  writeCodeFontFamily,
  writeCodeFontSizePx,
} from "./codeFontPreferences";

const SIZE_STORAGE_KEY = "omnigent:code-font-size";
const FAMILY_STORAGE_KEY = "omnigent:code-font-family";

afterEach(() => {
  localStorage.clear();
});

describe("codeFontPreferences — size", () => {
  it("returns the default when nothing is stored", () => {
    expect(readCodeFontSizePx()).toBe(CODE_FONT_SIZE_DEFAULT);
  });

  it("round-trips a valid size", () => {
    writeCodeFontSizePx(18);
    expect(readCodeFontSizePx()).toBe(18);
  });

  it("clamps a stored value above the range", () => {
    localStorage.setItem(SIZE_STORAGE_KEY, JSON.stringify(99));
    expect(readCodeFontSizePx()).toBe(CODE_FONT_SIZE_MAX);
  });

  it("clamps a stored value below the range", () => {
    localStorage.setItem(SIZE_STORAGE_KEY, JSON.stringify(2));
    expect(readCodeFontSizePx()).toBe(CODE_FONT_SIZE_MIN);
  });

  it("clamps out-of-range values on write", () => {
    writeCodeFontSizePx(40);
    expect(readCodeFontSizePx()).toBe(CODE_FONT_SIZE_MAX);
    writeCodeFontSizePx(1);
    expect(readCodeFontSizePx()).toBe(CODE_FONT_SIZE_MIN);
  });

  it("falls back to the default on malformed JSON", () => {
    // Corrupt localStorage should not break app boot.
    localStorage.setItem(SIZE_STORAGE_KEY, "}{not json");
    expect(readCodeFontSizePx()).toBe(CODE_FONT_SIZE_DEFAULT);
  });

  it("falls back to the default on a non-numeric value", () => {
    localStorage.setItem(SIZE_STORAGE_KEY, JSON.stringify("large"));
    expect(readCodeFontSizePx()).toBe(CODE_FONT_SIZE_DEFAULT);
  });
});

describe("codeFontPreferences — family", () => {
  it("returns the empty default when nothing is stored", () => {
    expect(readCodeFontFamily()).toBe(CODE_FONT_FAMILY_DEFAULT);
    expect(readCodeFontFamily()).toBe("");
  });

  it("round-trips a valid family name", () => {
    writeCodeFontFamily("Fira Code");
    expect(readCodeFontFamily()).toBe("Fira Code");
    expect(localStorage.getItem(FAMILY_STORAGE_KEY)).toBe(JSON.stringify("Fira Code"));
  });

  it("preserves spaces, commas and quotes in a font stack", () => {
    // A multi-family stack must survive normalization intact (the guard only
    // strips declaration-breaking chars, not the punctuation stacks rely on).
    writeCodeFontFamily('"JetBrains Mono", monospace');
    expect(readCodeFontFamily()).toBe('"JetBrains Mono", monospace');
  });

  it("trims surrounding whitespace", () => {
    writeCodeFontFamily("  Menlo  ");
    expect(readCodeFontFamily()).toBe("Menlo");
  });

  it("clears the preference when written empty or whitespace-only", () => {
    writeCodeFontFamily("Fira Code");
    expect(localStorage.getItem(FAMILY_STORAGE_KEY)).not.toBeNull();
    writeCodeFontFamily("   ");
    // Empty input removes the key rather than storing a blank string.
    expect(localStorage.getItem(FAMILY_STORAGE_KEY)).toBeNull();
    expect(readCodeFontFamily()).toBe("");
  });

  it("strips characters that could break the CSS declaration", () => {
    // `;{}` and control chars can't be allowed to escape the value we hand the
    // widget; everything else about the name (here the leading font) is kept.
    writeCodeFontFamily("Menlo;}body{");
    expect(readCodeFontFamily()).toBe("Menlobody");
  });

  it("falls back to the default on a value longer than the cap", () => {
    writeCodeFontFamily("x".repeat(200));
    expect(readCodeFontFamily()).toBe(CODE_FONT_FAMILY_DEFAULT);
    expect(localStorage.getItem(FAMILY_STORAGE_KEY)).toBeNull();
  });

  it("falls back to the default on malformed JSON", () => {
    // Corrupt localStorage should not break app boot.
    localStorage.setItem(FAMILY_STORAGE_KEY, "}{not json");
    expect(readCodeFontFamily()).toBe(CODE_FONT_FAMILY_DEFAULT);
  });

  it("falls back to the default on a non-string value", () => {
    localStorage.setItem(FAMILY_STORAGE_KEY, JSON.stringify(42));
    expect(readCodeFontFamily()).toBe(CODE_FONT_FAMILY_DEFAULT);
  });
});

describe("codeFontFamilyForEditor", () => {
  it("appends the mono fallback stack to a custom family", () => {
    // A custom name leads, with the app mono stack appended so an
    // uninstalled/partial name degrades to mono, not the widget default.
    expect(codeFontFamilyForEditor("Fira Code")).toBe(`Fira Code, ${CODE_FONT_FAMILY_FALLBACK}`);
  });

  it("resolves an empty family to the shared mono stack (uniform default)", () => {
    // Both Monaco and the terminal get the same mono stack when no custom family
    // is set, so their defaults match rather than each using its own built-in.
    expect(codeFontFamilyForEditor("")).toBe(CODE_FONT_FAMILY_FALLBACK);
    expect(codeFontFamilyForEditor("   ")).toBe(CODE_FONT_FAMILY_FALLBACK);
  });

  it("strips declaration-breaking chars before appending the fallback", () => {
    expect(codeFontFamilyForEditor("Menlo;}")).toBe(`Menlo, ${CODE_FONT_FAMILY_FALLBACK}`);
  });
});

describe("codeFontPreferences — pub/sub", () => {
  it("notifies subscribers with the new font when the size is written", () => {
    const cb = vi.fn();
    const unsubscribe = subscribeCodeFont(cb);

    writeCodeFontSizePx(20);
    // The callback receives the freshly persisted font so a mounted editor can
    // re-apply it live without re-reading storage itself.
    expect(cb).toHaveBeenCalledWith({ sizePx: 20, family: "" });

    unsubscribe();
  });

  it("notifies subscribers with the new font when the family is written", () => {
    const cb = vi.fn();
    const unsubscribe = subscribeCodeFont(cb);

    writeCodeFontFamily("Fira Code");
    expect(cb).toHaveBeenCalledWith({ sizePx: CODE_FONT_SIZE_DEFAULT, family: "Fira Code" });

    unsubscribe();
  });

  it("stops notifying after unsubscribe", () => {
    const cb = vi.fn();
    const unsubscribe = subscribeCodeFont(cb);
    unsubscribe();

    writeCodeFontSizePx(18);
    // A disposed editor's listener must not keep firing, or a stale closure
    // would touch a torn-down instance.
    expect(cb).not.toHaveBeenCalled();
  });

  it("reflects the clamped value in the emitted font", () => {
    const cb = vi.fn();
    const unsubscribe = subscribeCodeFont(cb);

    writeCodeFontSizePx(999);
    // Subscribers see the same clamped value that was persisted, so the live
    // widget and a later reload agree.
    expect(cb).toHaveBeenCalledWith({ sizePx: CODE_FONT_SIZE_MAX, family: "" });

    unsubscribe();
  });

  it("emits the intended size even when the storage write throws", () => {
    const cb = vi.fn();
    const unsubscribe = subscribeCodeFont(cb);
    // Storage disabled/quota-exceeded: the persist fails, but a mounted editor
    // must still re-font to the chosen size now instead of the stale value.
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    try {
      writeCodeFontSizePx(19);
      expect(cb).toHaveBeenCalledWith({ sizePx: 19, family: "" });
    } finally {
      setItem.mockRestore();
    }
    unsubscribe();
  });

  it("emits the intended family even when the storage write throws", () => {
    const cb = vi.fn();
    const unsubscribe = subscribeCodeFont(cb);
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    try {
      writeCodeFontFamily("Fira Code");
      expect(cb).toHaveBeenCalledWith({ sizePx: CODE_FONT_SIZE_DEFAULT, family: "Fira Code" });
    } finally {
      setItem.mockRestore();
    }
    unsubscribe();
  });
});

describe("readCodeFont", () => {
  it("reads size and family together", () => {
    writeCodeFontSizePx(15);
    writeCodeFontFamily("Menlo");
    expect(readCodeFont()).toEqual({ sizePx: 15, family: "Menlo" });
  });
});
