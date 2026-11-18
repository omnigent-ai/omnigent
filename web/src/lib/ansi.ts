// Terminal control sequences: 7-bit (ESC-introduced) and 8-bit (C1) CSI and
// OSC, single-character escapes, and stray C1 controls. An OSC missing its
// terminator ends at the line break, so it cannot swallow the rest of the text.
const CONTROL_SEQUENCE_RE =
  // eslint-disable-next-line no-control-regex -- matching terminal control characters
  /\u001b(?:\[[0-?]*[ -/]*[@-~]|\][^\u0007\u001b\n]*(?:\u0007|\u001b\\)?|[@-Z\\-_])?|\u009b[0-?]*[ -/]*[@-~]|\u009d[^\u0007\u009c\u001b\n]*[\u0007\u009c]?|[\u0080-\u009f]/g;

// Select Graphic Rendition: the color / bold / underline escapes.
// eslint-disable-next-line no-control-regex -- matching terminal control characters
const SGR_RE = /^\u001b\[[0-9;:]*m$/;

// Colon-form truecolor / 256-color (`38:2::255:0:0`, `48:5:208`), which the
// renderer only understands in the semicolon form.
const COLON_COLOR_RE = /([34]8):(?:2:(?:\d*:)?(\d+):(\d+):(\d+)|5:(\d+))/g;

// eslint-disable-next-line no-control-regex -- matching terminal control characters
const TRAILING_PARTIAL_ESCAPE_RE = /(?:\u001b(?:\[[0-?]*[ -/]*)?|\u009b[0-?]*[ -/]*)$/;

// Control characters other than tab and line breaks.
// eslint-disable-next-line no-control-regex -- matching terminal control characters
const INVISIBLE_CONTROL_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g;

/**
 * Keep only color escapes, in the form the `ansi` highlighter understands.
 * Cursor moves, line clears and titles mean nothing in a static transcript,
 * and git's short reset `ESC[m` is spelled out because the renderer ignores it.
 */
export function keepAnsiColors(text: string): string {
  return text.replace(CONTROL_SEQUENCE_RE, (sequence) => {
    if (!SGR_RE.test(sequence)) return "";
    if (sequence === "\u001b[m") return "\u001b[0m";
    return sequence.replace(COLON_COLOR_RE, (_all, target, red, green, blue, index) =>
      index === undefined ? `${target};2;${red};${green};${blue}` : `${target};5;${index}`,
    );
  });
}

/** Remove terminal control sequences, e.g. so copied output is safe to paste. */
export function stripAnsi(text: string): string {
  return text.replace(CONTROL_SEQUENCE_RE, "");
}

/** Drop an escape sequence cut off at the end of the text, e.g. by a preview limit. */
export function dropTrailingPartialEscape(text: string): string {
  return text.replace(TRAILING_PARTIAL_ESCAPE_RE, "");
}

/** Show control characters as `\uXXXX` so raw output can be read and copied safely. */
export function showControlCharacters(text: string): string {
  return text.replace(
    INVISIBLE_CONTROL_RE,
    (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}
