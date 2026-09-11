import { describe, expect, it } from "vitest";
import { CodexTerminalPalette, codexTerminalTheme } from "./CodexTerminalPalette";

const encoder = new TextEncoder();
const decoder = new TextDecoder();

function rewrite(text: string): string {
  return decoder.decode(new CodexTerminalPalette().write(encoder.encode(text)));
}

describe("CodexTerminalPalette", () => {
  it.each(["30;30;30", "47;49;50", "244;244;244"])(
    "normalizes the cached input background %s to a themeable index",
    (color) => {
      expect(rewrite(`\x1b[48;2;${color}mhello\x1b[0m`)).toBe("\x1b[48;5;255mhello\x1b[0m");
    },
  );

  it.each([8, 234, 236, 255])("supports indexed input background %i", (index) => {
    expect(rewrite(`\x1b[48;5;${index}mhello`)).toBe("\x1b[48;5;255mhello");
  });

  it("supports older ANSI gray input bands", () => {
    expect(rewrite("\x1b[100mhello")).toBe("\x1b[48;5;255mhello");
  });

  it("preserves foregrounds, underlines, and colored backgrounds", () => {
    const input = "\x1b[1;38;2;244;244;244;48;2;12;45;67;58;2;30;30;30mcode\x1b[0m";
    expect(rewrite(input)).toBe(input);
  });

  it("preserves the original foreground and underline color of the reserved index", () => {
    expect(rewrite("\x1b[38;5;255;58;5;255;48;5;255mtext")).toBe(
      "\x1b[38;2;238;238;238;58;2;238;238;238;48;5;255mtext",
    );
  });

  it("handles combined attributes without mistaking foreground components for backgrounds", () => {
    expect(rewrite("\x1b[1;38;2;48;2;30;48;2;30;30;30;4mtext")).toBe(
      "\x1b[1;38;2;48;2;30;48;5;255;4mtext",
    );
  });

  it.each(["48:2:244:244:244", "48:2::244:244:244", "48:2:0:244:244:244", "48:5:255"])(
    "handles colon-separated colors: %s",
    (parameters) => {
      expect(rewrite(`\x1b[1;${parameters};4mtext`)).toBe("\x1b[1;48;5;255;4mtext");
    },
  );

  it.each(["48;2;244;244", "48;2;244;244;999", "48:2:1:244:244:244", "48;9;30;30;30"])(
    "leaves malformed or unsupported colors unchanged: %s",
    (parameters) => {
      const input = `\x1b[${parameters}mtext`;
      expect(rewrite(input)).toBe(input);
    },
  );

  it("handles every frame boundary without corrupting UTF-8 or terminal controls", () => {
    const input = encoder.encode(
      "hello λ🐈\x1b[1;48;2;244;244;244m世界\x1b[0m\x1b[2K\x1b[?1049hpopup",
    );
    const expected = "hello λ🐈\x1b[1;48;5;255m世界\x1b[0m\x1b[2K\x1b[?1049hpopup";
    for (let boundary = 0; boundary <= input.length; boundary++) {
      const palette = new CodexTerminalPalette();
      const first = palette.write(input.slice(0, boundary));
      const second = palette.write(input.slice(boundary));
      expect(decoder.decode(Uint8Array.from([...first, ...second]))).toBe(expected);
    }
    const palette = new CodexTerminalPalette();
    const result = Array.from(input).flatMap((byte) => [...palette.write(Uint8Array.of(byte))]);
    expect(decoder.decode(Uint8Array.from(result))).toBe(expected);
  });

  it.each([
    "\x1b]0;title\x1b[48;2;244;244;244m\x07",
    "\x1b]8;;https://example.com/\x1b[48;2;244;244;244m\x1b\\",
    "\x1bPpayload\x07\x1b[48;2;244;244;244m\x1b\\",
    "\x1b_payload\x1b[48;2;244;244;244m\x1b\\",
    "\x1b^payload\x1b[48;2;244;244;244m\x1b\\",
  ])("does not rewrite escape sequences inside control strings", (control) => {
    const input = encoder.encode(`${control}\x1b[48;2;244;244;244mtext`);
    const palette = new CodexTerminalPalette();
    const result = Array.from(input).flatMap((byte) => [...palette.write(Uint8Array.of(byte))]);
    expect(decoder.decode(Uint8Array.from(result))).toBe(`${control}\x1b[48;5;255mtext`);
  });

  it("does not buffer unbounded or aborted control sequences", () => {
    const long = `\x1b[${"1;".repeat(5000)}m`;
    const canceled = "\x1b[48;2\x18";
    expect(rewrite(`${long}${canceled}\x1b[48;2;30;30;30mtext`)).toBe(
      `${long}${canceled}\x1b[48;5;255mtext`,
    );
    expect(rewrite("\x1b[48;2\x1b[48;2;30;30;30mtext")).toBe("\x1b[48;2\x1b[48;5;255mtext");
    expect(rewrite("\x1b\x1b[48;2;30;30;30mtext")).toBe("\x1b\x1b[48;5;255mtext");
  });

  it("leaves ordinary bytes untouched", () => {
    const input = Uint8Array.of(0, 1, 7, 8, 9, 13, 127, 200, 255);
    expect(new CodexTerminalPalette().write(input)).toBe(input);
  });
});

describe("codexTerminalTheme", () => {
  it("changes only the input background slot while preserving the base theme", () => {
    const original = { foreground: "#123456", background: "#abcdef", red: "#ff0044" };
    for (const isDark of [false, true]) {
      const theme = codexTerminalTheme(original, isDark);
      expect(theme).toMatchObject(original);
      expect(theme.extendedAnsi?.[239]).toBe(isDark ? "#2f3132" : "#f4f4f4");
      expect(theme.extendedAnsi?.filter(Boolean)).toHaveLength(1);
    }
    expect(original).not.toHaveProperty("extendedAnsi");
  });
});
