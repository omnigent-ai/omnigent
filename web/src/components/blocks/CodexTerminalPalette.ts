import type { ITheme } from "@xterm/xterm";

export const CODEX_INPUT_BACKGROUND_INDEX = 255;
const INPUT_BACKGROUNDS = new Set(["30;30;30", "47;49;50", "244;244;244"]);
const INDEXED_INPUT_BACKGROUNDS = new Set([8, 234, 236, 255]);
const MAX_SEQUENCE_LENGTH = 128;
const encoder = new TextEncoder();

export function codexTerminalTheme(theme: ITheme, isDark: boolean): ITheme {
  const extendedAnsi = new Array<string>(240);
  extendedAnsi[CODEX_INPUT_BACKGROUND_INDEX - 16] = isDark ? "#2f3132" : "#f4f4f4";
  return { ...theme, extendedAnsi };
}

function colorSequence(kind: number, components: string[]): string | null {
  if (!components.every((component) => /^\d+$/.test(component))) return null;
  const values = components.map(Number);
  if (values.some((value) => value > 255)) return null;
  if (kind === 48 && INPUT_BACKGROUNDS.has(values.join(";"))) {
    return `48;5;${CODEX_INPUT_BACKGROUND_INDEX}`;
  }
  return `${kind};2;${components.join(";")}`;
}

function indexedSequence(kind: number, index: number): string {
  if (kind === 48 && INDEXED_INPUT_BACKGROUNDS.has(index)) {
    return `48;5;${CODEX_INPUT_BACKGROUND_INDEX}`;
  }
  if (index === CODEX_INPUT_BACKGROUND_INDEX) return `${kind};2;238;238;238`;
  return `${kind};5;${index}`;
}

function rewriteSgr(sequence: number[]): Uint8Array {
  const original = Uint8Array.from(sequence);
  if (sequence.at(-1) !== 109) return original;
  const body = String.fromCharCode(...sequence.slice(2, -1));
  if (!/^[\d:;]*$/.test(body)) return original;
  const parameters = body.split(";");
  const rewritten: string[] = [];
  for (let index = 0; index < parameters.length; index++) {
    const parameter = parameters[index];
    const parts = parameter.split(":");
    const kind = Number(parts[0]);
    if (kind === 100 && parts.length === 1) {
      rewritten.push(`48;5;${CODEX_INPUT_BACKGROUND_INDEX}`);
    } else if (kind === 38 || kind === 48 || kind === 58) {
      if (parts.length > 1) {
        if (parts[1] === "5" && parts.length === 3 && /^\d+$/.test(parts[2])) {
          rewritten.push(indexedSequence(kind, Number(parts[2])));
        } else if (
          parts[1] === "2" &&
          (parts.length === 5 || (parts.length === 6 && ["", "0"].includes(parts[2])))
        ) {
          const replacement = colorSequence(kind, parts.slice(-3));
          if (replacement === null) return original;
          rewritten.push(replacement);
        } else {
          return original;
        }
      } else {
        const mode = parameters[++index];
        if (mode === "5" && /^\d+$/.test(parameters[index + 1] ?? "")) {
          rewritten.push(indexedSequence(kind, Number(parameters[++index])));
        } else if (mode === "2" && index + 3 < parameters.length) {
          const replacement = colorSequence(kind, parameters.slice(index + 1, index + 4));
          if (replacement === null) return original;
          rewritten.push(replacement);
          index += 3;
        } else {
          return original;
        }
      }
    } else {
      rewritten.push(parameter);
    }
  }
  return encoder.encode(`\x1b[${rewritten.join(";")}m`);
}

/** Give Codex's cached input backgrounds a palette slot that can change without a repaint. */
export class CodexTerminalPalette {
  private state: "text" | "escape" | "csi" | "string" | "string-escape" = "text";
  private sequence: number[] = [];
  private osc = false;

  write(bytes: Uint8Array): Uint8Array {
    if (this.state === "text" && !bytes.includes(27)) return bytes;
    const output: number[] = [];
    for (const byte of bytes) {
      switch (this.state) {
        case "text":
          if (byte === 27) {
            this.sequence = [byte];
            this.state = "escape";
          } else {
            output.push(byte);
          }
          break;
        case "escape":
          if (byte === 91) {
            this.sequence.push(byte);
            this.state = "csi";
          } else {
            output.push(...this.sequence);
            this.sequence = [];
            if (byte === 27) {
              this.sequence = [byte];
            } else {
              output.push(byte);
              this.osc = byte === 93;
              this.state = [93, 80, 95, 94, 88].includes(byte) ? "string" : "text";
            }
          }
          break;
        case "csi":
          if (byte === 27) {
            output.push(...this.sequence);
            this.sequence = [byte];
            this.state = "escape";
          } else {
            this.sequence.push(byte);
            if (byte >= 64 && byte <= 126) {
              output.push(...rewriteSgr(this.sequence));
              this.sequence = [];
              this.state = "text";
            } else if ([24, 26].includes(byte) || this.sequence.length >= MAX_SEQUENCE_LENGTH) {
              output.push(...this.sequence);
              this.sequence = [];
              this.state = "text";
            }
          }
          break;
        case "string":
        case "string-escape":
          output.push(byte);
          if (
            (this.state === "string-escape" && byte === 92) ||
            (this.osc && byte === 7) ||
            byte === 24 ||
            byte === 26
          ) {
            this.state = "text";
          } else {
            this.state = byte === 27 ? "string-escape" : "string";
          }
          break;
      }
    }
    return Uint8Array.from(output);
  }
}
