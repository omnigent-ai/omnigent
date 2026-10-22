import { Terminal } from "@xterm/xterm";
import { describe, expect, it } from "vitest";
import { CodexTerminalPalette, codexTerminalTheme } from "./CodexTerminalPalette";

const encoder = new TextEncoder();
const decoder = new TextDecoder();
const ESCAPE = "\x1b";
const BELL = "\x07";
const CANCEL = "\x18";
const STRING_TERMINATOR = `${ESCAPE}\\`;
const CONTROL_STRINGS = [
  { name: "window title", prefix: `${ESCAPE}]0;title`, terminator: BELL },
  { name: "hyperlink", prefix: `${ESCAPE}]8;;https://example.com/`, terminator: STRING_TERMINATOR },
  {
    name: "device control string",
    prefix: `${ESCAPE}Pqpayload${BELL}`,
    terminator: STRING_TERMINATOR,
  },
  { name: "application command", prefix: `${ESCAPE}_payload`, terminator: STRING_TERMINATOR },
  { name: "privacy message", prefix: `${ESCAPE}^payload`, terminator: STRING_TERMINATOR },
  { name: "start of string", prefix: `${ESCAPE}Xpayload`, terminator: STRING_TERMINATOR },
];
const CACHED_INPUT_RGB = {
  blackTerminal: [30, 30, 30],
  darkTerminal: [47, 49, 50],
  lightTerminal: [244, 244, 244],
};

function sgr(...attributes: (number | string)[]): string {
  return `${ESCAPE}[${attributes.join(";")}m`;
}

function rgbBackground(rgb: readonly number[]): string {
  return sgr(48, 2, ...rgb);
}

function indexedBackground(index: number): string {
  return sgr(48, 5, index);
}

const RESET_STYLE = sgr(0);
const THEMED_INPUT_BACKGROUND = indexedBackground(255);

function rewrite(text: string): string {
  return decoder.decode(new CodexTerminalPalette().write(encoder.encode(text)));
}

function rewriteFrames(frames: Uint8Array[]): string {
  const palette = new CodexTerminalPalette();
  const output = frames.flatMap((frame) => Array.from(palette.write(frame)));
  return decoder.decode(Uint8Array.from(output));
}

async function renderCell(text: string) {
  const terminal = new Terminal({ allowProposedApi: true });
  try {
    await new Promise<void>((resolve) => {
      terminal.write(encoder.encode(text), resolve);
    });
    return terminal.buffer.active.getLine(0)!.getCell(0)!;
  } finally {
    terminal.dispose();
  }
}

describe("Codex input backgrounds", () => {
  it.each([
    { name: "black terminal", rgb: CACHED_INPUT_RGB.blackTerminal },
    { name: "dark terminal", rgb: CACHED_INPUT_RGB.darkTerminal },
    { name: "light terminal", rgb: CACHED_INPUT_RGB.lightTerminal },
  ])("makes the cached background from a $name themeable", ({ rgb }) => {
    const input = `${rgbBackground(rgb)}hello${RESET_STYLE}`;
    expect(rewrite(input)).toBe(`${THEMED_INPUT_BACKGROUND}hello${RESET_STYLE}`);
  });

  it.each([8, 236, 255])("also adapts cached palette index %i", (index) => {
    expect(rewrite(`${indexedBackground(index)}hello`)).toBe(`${THEMED_INPUT_BACKGROUND}hello`);
  });

  it("also adapts the older ANSI bright-black background", () => {
    expect(rewrite(`${sgr(100)}hello`)).toBe(`${THEMED_INPUT_BACKGROUND}hello`);
  });
});

describe("Codex 0.153.4 picker backgrounds", () => {
  it.each([
    { name: "light selection", rgb: [224, 224, 224], index: 254 },
    { name: "dark stripe", rgb: [31, 33, 35], index: 253 },
    { name: "black-terminal stripe", rgb: [14, 14, 14], index: 253 },
  ])("adapts the $name without using the input shade", ({ rgb, index }) => {
    expect(rewrite(`${rgbBackground(rgb)}row`)).toBe(`${indexedBackground(index)}row`);
  });

  it.each([
    { source: 233, target: 253 },
    { source: 234, target: 253 },
    { source: 254, target: 254 },
  ])("adapts picker palette index $source to $target", ({ source, target }) => {
    expect(rewrite(`${indexedBackground(source)}row`)).toBe(`${indexedBackground(target)}row`);
  });

  it("keeps dark 256-color stripes and selection in different palette slots", () => {
    const input = `${indexedBackground(234)}stripe${indexedBackground(236)}selected`;
    expect(rewrite(input)).toBe(`${indexedBackground(253)}stripe${indexedBackground(255)}selected`);
  });

  it("adapts picker colors across split colon-separated sequences", () => {
    const input = encoder.encode(`${sgr("48:2::224:224:224")}selected${sgr("48:5:234")}stripe`);
    const frames = Array.from(input, (byte) => Uint8Array.of(byte));
    expect(rewriteFrames(frames)).toBe(
      `${indexedBackground(254)}selected${indexedBackground(253)}stripe`,
    );
  });
});

describe("colors that must stay unchanged", () => {
  it("does not give default-background sessions a fixed background", () => {
    const input = `${sgr(49)}text${RESET_STYLE}`;
    expect(rewrite(input)).toBe(input);
  });

  it("preserves RGB foregrounds, underlines, and unrelated backgrounds", () => {
    const attributes = sgr(
      1,
      38,
      2,
      ...CACHED_INPUT_RGB.lightTerminal,
      48,
      2,
      12,
      45,
      67,
      58,
      2,
      ...CACHED_INPUT_RGB.blackTerminal,
    );
    const input = `${attributes}code${RESET_STYLE}`;
    expect(rewrite(input)).toBe(input);
  });

  it("keeps foreground and underline index 255 at their original RGB color", () => {
    const input = `${sgr(38, 5, 255, 58, 5, 255, 48, 5, 255)}text`;
    const expected = `${sgr(38, 2, 238, 238, 238, 58, 2, 238, 238, 238, 48, 5, 255)}text`;
    expect(rewrite(input)).toBe(expected);
  });

  it.each([
    { index: 253, gray: 218 },
    { index: 254, gray: 228 },
    { index: 255, gray: 238 },
  ])("preserves foreground and underline colors from reserved slot $index", ({ index, gray }) => {
    const input = `${sgr(38, 5, index, 58, 5, index)}text`;
    expect(rewrite(input)).toBe(`${sgr(38, 2, gray, gray, gray, 58, 2, gray, gray, gray)}text`);
  });

  it("preserves an unrelated background that uses a reserved slot", () => {
    expect(rewrite(`${indexedBackground(253)}text`)).toBe(`${rgbBackground([218, 218, 218])}text`);
  });

  it("does not mistake RGB components for separate background attributes", () => {
    const input = `${sgr(1, 38, 2, 48, 2, 30, 48, 2, ...CACHED_INPUT_RGB.blackTerminal, 4)}text`;
    expect(rewrite(input)).toBe(`${sgr(1, 38, 2, 48, 2, 30, 48, 5, 255, 4)}text`);
  });
});

describe("ANSI color syntax", () => {
  it.each([
    { name: "RGB without a color-space field", parameters: "48:2:244:244:244" },
    { name: "RGB with an empty color-space field", parameters: "48:2::244:244:244" },
    { name: "RGB with the default color space", parameters: "48:2:0:244:244:244" },
    { name: "an indexed color", parameters: "48:5:255" },
  ])("accepts colon-separated $name", ({ parameters }) => {
    expect(rewrite(`${sgr(1, parameters, 4)}text`)).toBe(`${sgr(1, 48, 5, 255, 4)}text`);
  });

  it.each([
    { name: "incomplete RGB", parameters: "48;2;244;244" },
    { name: "out-of-range RGB", parameters: "48;2;244;244;999" },
    { name: "an unsupported color space", parameters: "48:2:1:244:244:244" },
    { name: "an unknown color mode", parameters: "48;9;30;30;30" },
  ])("passes through $name unchanged", ({ parameters }) => {
    const input = `${sgr(parameters)}text`;
    expect(rewrite(input)).toBe(input);
  });
});

describe.each([";", ":"])("out-of-range palette indices with %s separators", (separator) => {
  it.each([
    { index: 509, gray: 218 },
    { index: 510, gray: 228 },
    { index: 511, gray: 238 },
    { index: 2147483647, gray: 238 },
    { index: 2147483648, gray: 238 },
    { index: 4294967293, gray: 238 },
    { index: "99999999999999999999", gray: 238 },
  ])("preserves foreground and underline colors for index $index", ({ index, gray }) => {
    const foreground = [38, 5, index].join(separator);
    const underline = [58, 5, index].join(separator);
    expect(rewrite(sgr(foreground, underline))).toBe(
      sgr(38, 2, gray, gray, gray, 58, 2, gray, gray, gray),
    );
  });

  it.each([
    { index: 256, expected: indexedBackground(0) },
    { index: 264, expected: indexedBackground(255) },
    { index: 490, expected: indexedBackground(253) },
    { index: 492, expected: indexedBackground(255) },
    { index: 509, expected: rgbBackground([218, 218, 218]) },
    { index: 510, expected: indexedBackground(254) },
    { index: 511, expected: indexedBackground(255) },
    { index: 2147483648, expected: indexedBackground(255) },
  ])("normalizes background index $index before mapping it", ({ index, expected }) => {
    expect(rewrite(sgr([48, 5, index].join(separator)))).toBe(expected);
  });
});

describe("streaming terminal output", () => {
  const splitFrameInput = encoder.encode(
    `hello λ🐈${sgr(1, 48, 2, ...CACHED_INPUT_RGB.lightTerminal)}世界` +
      `${RESET_STYLE}${ESCAPE}[2K${ESCAPE}[?1049hpopup`,
  );
  const splitFrameExpected = `hello λ🐈${sgr(1, 48, 5, 255)}世界${RESET_STYLE}${ESCAPE}[2K${ESCAPE}[?1049hpopup`;

  it("preserves UTF-8 and terminal controls at every possible frame boundary", () => {
    for (let boundary = 0; boundary <= splitFrameInput.length; boundary++) {
      const frames = [splitFrameInput.slice(0, boundary), splitFrameInput.slice(boundary)];
      expect(rewriteFrames(frames)).toBe(splitFrameExpected);
    }
  });

  it("also works when each frame contains just one byte", () => {
    const frames = Array.from(splitFrameInput, (byte) => Uint8Array.of(byte));
    expect(rewriteFrames(frames)).toBe(splitFrameExpected);
  });

  it.each(CONTROL_STRINGS)(
    "preserves printable $name payloads, including UTF-8 continuation bytes",
    ({ prefix, terminator }) => {
      const payload = `${prefix}śŜ[48;2;244;244;244m${terminator}`;
      const input = encoder.encode(
        `${payload}${rgbBackground(CACHED_INPUT_RGB.lightTerminal)}text`,
      );
      const frames = Array.from(input, (byte) => Uint8Array.of(byte));
      expect(rewriteFrames(frames)).toBe(`${payload}${THEMED_INPUT_BACKGROUND}text`);
    },
  );

  it.each(CONTROL_STRINGS)("recovers from an ESC-interrupted $name", ({ prefix }) => {
    const input = encoder.encode(`${prefix}${rgbBackground(CACHED_INPUT_RGB.lightTerminal)}text`);
    const expected = `${prefix}${THEMED_INPUT_BACKGROUND}text`;
    for (let boundary = 0; boundary <= input.length; boundary++) {
      expect(rewriteFrames([input.slice(0, boundary), input.slice(boundary)])).toBe(expected);
    }
    expect(rewriteFrames(Array.from(input, (byte) => Uint8Array.of(byte)))).toBe(expected);
  });

  it("flushes overlong sequences instead of buffering without a limit", () => {
    const overlong = sgr("1;".repeat(5000));
    const canceled = `${ESCAPE}[48;2${CANCEL}`;
    const input = `${overlong}${canceled}${rgbBackground(CACHED_INPUT_RGB.blackTerminal)}text`;
    expect(rewrite(input)).toBe(`${overlong}${canceled}${THEMED_INPUT_BACKGROUND}text`);
  });

  it.each([
    { name: "an interrupted color sequence", prefix: `${ESCAPE}[48;2` },
    { name: "an extra escape byte", prefix: ESCAPE },
  ])("recovers after $name", ({ prefix }) => {
    const input = `${prefix}${rgbBackground(CACHED_INPUT_RGB.blackTerminal)}text`;
    expect(rewrite(input)).toBe(`${prefix}${THEMED_INPUT_BACKGROUND}text`);
  });

  it("passes ordinary bytes through without allocating a replacement", () => {
    const input = Uint8Array.of(0, 1, 7, 8, 9, 13, 127, 200, 255);
    expect(new CodexTerminalPalette().write(input)).toBe(input);
  });
});

describe("xterm compatibility", () => {
  it.each(CONTROL_STRINGS)(
    "recolors visible cells after an ESC-interrupted $name",
    async ({ prefix }) => {
      const input = `${prefix}${rgbBackground(CACHED_INPUT_RGB.lightTerminal)}X`;
      const original = await renderCell(input);
      const adapted = await renderCell(rewrite(input));
      expect(original.getChars()).toBe("X");
      expect(original.isBgRGB()).toBe(true);
      expect(original.getBgColor()).toBe(0xf4f4f4);
      expect(adapted.getChars()).toBe("X");
      expect(adapted.isBgPalette()).toBe(true);
      expect(adapted.getBgColor()).toBe(255);
    },
  );

  it.each([
    { index: 509, paletteIndex: 253, rgb: 0xdadada },
    { index: 2147483648, paletteIndex: 255, rgb: 0xeeeeee },
    { index: 4294967293, paletteIndex: 255, rgb: 0xeeeeee },
  ])(
    "matches xterm's clamping and wrapping of foreground index $index",
    async ({ index, paletteIndex, rgb }) => {
      const input = `${sgr(38, 5, index)}X`;
      const original = await renderCell(input);
      const adapted = await renderCell(rewrite(input));
      expect(original.isFgPalette()).toBe(true);
      expect(original.getFgColor()).toBe(paletteIndex);
      expect(adapted.isFgRGB()).toBe(true);
      expect(adapted.getFgColor()).toBe(rgb);
    },
  );
});

describe("terminal theme", () => {
  it("changes only the three reserved background slots, without mutating the base theme", () => {
    const original = { foreground: "#123456", background: "#abcdef", red: "#ff0044" };
    for (const isDark of [false, true]) {
      const theme = codexTerminalTheme(original, isDark);
      expect(theme).toMatchObject(original);
      expect(theme.extendedAnsi?.[253 - 16]).toBe(isDark ? "#1f2123" : "#fafafa");
      expect(theme.extendedAnsi?.[254 - 16]).toBe(isDark ? "#464849" : "#e0e0e0");
      expect(theme.extendedAnsi?.[255 - 16]).toBe(isDark ? "#2f3132" : "#f4f4f4");
      expect(theme.extendedAnsi?.filter(Boolean)).toHaveLength(3);
      expect(theme.extendedAnsi?.slice(0, 253 - 16).every((color) => color === undefined)).toBe(
        true,
      );
    }
    expect(original).not.toHaveProperty("extendedAnsi");
  });
});
