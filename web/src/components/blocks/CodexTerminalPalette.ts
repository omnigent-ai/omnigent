import type { ITheme } from "@xterm/xterm";

export const CODEX_INPUT_BACKGROUND_INDEX = 255;
const CODEX_CACHED_INPUT_RGB = {
  blackTerminal: "30;30;30",
  darkTerminal: "47;49;50",
  lightTerminal: "244;244;244",
};
const INPUT_BACKGROUNDS = new Set(Object.values(CODEX_CACHED_INPUT_RGB));
const INDEXED_INPUT_BACKGROUNDS = new Set([8, 234, 236, 255]);
const THEMED_INPUT_BACKGROUND = `48;5;${CODEX_INPUT_BACKGROUND_INDEX}`;
const ORIGINAL_INDEX_255_RGB = "238;238;238";
const STANDARD_ANSI_COLOR_COUNT = 16;
const EXTENDED_ANSI_COLOR_COUNT = 240;

const ESCAPE = 0x1b;
const BELL = 0x07;
const CANCEL = 0x18;
const SUBSTITUTE = 0x1a;
const CONTROL_SEQUENCE_START = "[".charCodeAt(0);
const STRING_TERMINATOR = "\\".charCodeAt(0);
const SET_GRAPHICS_RENDITION = "m".charCodeAt(0);
const CONTROL_STRING_START = {
  operatingSystemCommand: "]".charCodeAt(0),
  deviceControlString: "P".charCodeAt(0),
  applicationProgramCommand: "_".charCodeAt(0),
  privacyMessage: "^".charCodeAt(0),
  startOfString: "X".charCodeAt(0),
};
const CONTROL_STRING_START_BYTES = new Set(Object.values(CONTROL_STRING_START));
const COLOR_ATTRIBUTE = { foreground: 38, background: 48, underline: 58 };
const BRIGHT_BLACK_BACKGROUND = 100;
const COLOR_MODE = { rgb: "2", indexed: "5" };
const MAX_CONTROL_SEQUENCE_LENGTH = 128;
const encoder = new TextEncoder();

export function codexTerminalTheme(theme: ITheme, isDark: boolean): ITheme {
  const extendedAnsi = new Array<string>(EXTENDED_ANSI_COLOR_COUNT);
  const inputBackgroundOffset = CODEX_INPUT_BACKGROUND_INDEX - STANDARD_ANSI_COLOR_COUNT;
  extendedAnsi[inputBackgroundOffset] = isDark ? "#2f3132" : "#f4f4f4";
  return { ...theme, extendedAnsi };
}

function isDecimal(parameter: string | undefined): parameter is string {
  return parameter !== undefined && /^\d+$/.test(parameter);
}

function rewriteRgbColor(attribute: number, components: string[]): string | null {
  if (components.length !== 3 || !components.every(isDecimal)) return null;
  const values = components.map(Number);
  if (values.some((value) => value > 255)) return null;
  if (attribute === COLOR_ATTRIBUTE.background && INPUT_BACKGROUNDS.has(values.join(";"))) {
    return THEMED_INPUT_BACKGROUND;
  }
  return `${attribute};${COLOR_MODE.rgb};${components.join(";")}`;
}

function rewriteIndexedColor(attribute: number, paletteIndex: number): string {
  if (attribute === COLOR_ATTRIBUTE.background && INDEXED_INPUT_BACKGROUNDS.has(paletteIndex)) {
    return THEMED_INPUT_BACKGROUND;
  }
  if (paletteIndex === CODEX_INPUT_BACKGROUND_INDEX) {
    return `${attribute};${COLOR_MODE.rgb};${ORIGINAL_INDEX_255_RGB}`;
  }
  return `${attribute};${COLOR_MODE.indexed};${paletteIndex}`;
}

function rewriteColonColor(attribute: number, colorParts: string[]): string | null {
  const [mode, ...components] = colorParts;
  if (mode === COLOR_MODE.indexed && components.length === 1 && isDecimal(components[0])) {
    return rewriteIndexedColor(attribute, Number(components[0]));
  }
  if (mode === COLOR_MODE.rgb) {
    const hasDefaultColorSpace = components.length === 4 && ["", "0"].includes(components[0]);
    return rewriteRgbColor(attribute, hasDefaultColorSpace ? components.slice(1) : components);
  }
  return null;
}

function rewriteGraphicsRendition(sequence: number[]): Uint8Array {
  const original = Uint8Array.from(sequence);
  if (sequence.at(-1) !== SET_GRAPHICS_RENDITION) return original;
  const body = String.fromCharCode(...sequence.slice(2, -1));
  if (!/^[\d:;]*$/.test(body)) return original;
  const parameters = body.split(";");
  const rewritten: string[] = [];
  for (let index = 0; index < parameters.length; index++) {
    const parameter = parameters[index];
    const parts = parameter.split(":");
    const attribute = Number(parts[0]);
    if (attribute === BRIGHT_BLACK_BACKGROUND && parts.length === 1) {
      rewritten.push(THEMED_INPUT_BACKGROUND);
      continue;
    }
    if (!Object.values(COLOR_ATTRIBUTE).includes(attribute)) {
      rewritten.push(parameter);
      continue;
    }
    if (parts.length > 1) {
      const replacement = rewriteColonColor(attribute, parts.slice(1));
      if (replacement === null) return original;
      rewritten.push(replacement);
      continue;
    }

    const mode = parameters[index + 1];
    if (mode === COLOR_MODE.indexed && isDecimal(parameters[index + 2])) {
      rewritten.push(rewriteIndexedColor(attribute, Number(parameters[index + 2])));
      index += 2;
    } else if (mode === COLOR_MODE.rgb) {
      const replacement = rewriteRgbColor(attribute, parameters.slice(index + 2, index + 5));
      if (replacement === null) return original;
      rewritten.push(replacement);
      index += 4;
    } else {
      return original;
    }
  }
  return encoder.encode(`\x1b[${rewritten.join(";")}m`);
}

function isControlSequenceFinalByte(byte: number): boolean {
  return byte >= 0x40 && byte <= 0x7e;
}

function isCanceled(byte: number): boolean {
  return byte === CANCEL || byte === SUBSTITUTE;
}

type ParserState =
  "text" | "escape" | "control-sequence" | "control-string" | "control-string-escape";

/** Give Codex's cached input backgrounds a palette slot that can change without a repaint. */
export class CodexTerminalPalette {
  private state: ParserState = "text";
  private pendingSequence: number[] = [];
  private controlStringAllowsBell = false;

  write(bytes: Uint8Array): Uint8Array {
    if (this.state === "text" && !bytes.includes(ESCAPE)) return bytes;
    const output: number[] = [];
    for (const byte of bytes) {
      switch (this.state) {
        case "text":
          if (byte === ESCAPE) {
            this.pendingSequence = [byte];
            this.state = "escape";
          } else {
            output.push(byte);
          }
          break;
        case "escape":
          if (byte === CONTROL_SEQUENCE_START) {
            this.pendingSequence.push(byte);
            this.state = "control-sequence";
          } else {
            output.push(...this.pendingSequence);
            this.pendingSequence = [];
            if (byte === ESCAPE) {
              this.pendingSequence = [byte];
            } else {
              output.push(byte);
              this.controlStringAllowsBell = byte === CONTROL_STRING_START.operatingSystemCommand;
              this.state = CONTROL_STRING_START_BYTES.has(byte) ? "control-string" : "text";
            }
          }
          break;
        case "control-sequence":
          if (byte === ESCAPE) {
            output.push(...this.pendingSequence);
            this.pendingSequence = [byte];
            this.state = "escape";
          } else {
            this.pendingSequence.push(byte);
            if (isControlSequenceFinalByte(byte)) {
              output.push(...rewriteGraphicsRendition(this.pendingSequence));
              this.pendingSequence = [];
              this.state = "text";
            } else if (
              isCanceled(byte) ||
              this.pendingSequence.length >= MAX_CONTROL_SEQUENCE_LENGTH
            ) {
              output.push(...this.pendingSequence);
              this.pendingSequence = [];
              this.state = "text";
            }
          }
          break;
        case "control-string":
        case "control-string-escape":
          output.push(byte);
          if (
            (this.state === "control-string-escape" && byte === STRING_TERMINATOR) ||
            (this.controlStringAllowsBell && byte === BELL) ||
            isCanceled(byte)
          ) {
            this.state = "text";
          } else {
            this.state = byte === ESCAPE ? "control-string-escape" : "control-string";
          }
          break;
      }
    }
    return Uint8Array.from(output);
  }
}
