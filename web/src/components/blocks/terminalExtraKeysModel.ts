// Pure key table, byte encoder and sticky-modifier reducer for the terminal
// extra-keys row (Termux-style Esc / Tab / Ctrl / arrows bar for soft
// keyboards). No DOM or xterm dependency so the whole matrix is unit-testable.

/** Kitty Keyboard Protocol / CSI-u encoding for Shift+Enter. */
export const SHIFT_ENTER_CSI_U = "\x1b[13;2u";

export type ModifierId = "ctrl" | "alt" | "shift";

export type PlainKeyId =
  "esc" | "shift-tab" | "tab" | "home" | "up" | "end" | "pgup" | "left" | "down" | "right" | "pgdn";

interface ExtraKeyBase {
  /** Glyph shown on the key. */
  label: string;
  /** Accessible name (glyphs alone read poorly to screen readers). */
  name: string;
}

export type ExtraKeyDef =
  | (ExtraKeyBase & { id: PlainKeyId; kind: "key" })
  | (ExtraKeyBase & { id: ModifierId; kind: "modifier" });

/** Fixed Termux-style 2×7 grid; positions never move across widths. */
export const EXTRA_KEY_ROWS: readonly (readonly ExtraKeyDef[])[] = [
  [
    { id: "esc", label: "Esc", name: "Escape", kind: "key" },
    { id: "shift-tab", label: "⇧Tab", name: "Shift Tab", kind: "key" },
    { id: "tab", label: "Tab", name: "Tab", kind: "key" },
    { id: "home", label: "Home", name: "Home", kind: "key" },
    { id: "up", label: "↑", name: "Arrow up", kind: "key" },
    { id: "end", label: "End", name: "End", kind: "key" },
    { id: "pgup", label: "PgUp", name: "Page up", kind: "key" },
  ],
  [
    { id: "ctrl", label: "Ctrl", name: "Control", kind: "modifier" },
    { id: "alt", label: "Alt", name: "Alt", kind: "modifier" },
    { id: "shift", label: "Shift", name: "Shift", kind: "modifier" },
    { id: "left", label: "←", name: "Arrow left", kind: "key" },
    { id: "down", label: "↓", name: "Arrow down", kind: "key" },
    { id: "right", label: "→", name: "Arrow right", kind: "key" },
    { id: "pgdn", label: "PgDn", name: "Page down", kind: "key" },
  ],
];

/** Holding a modifier this long locks it instead of arming it for one key. */
export const LONG_PRESS_MS = 500;

export interface Modifiers {
  ctrl: boolean;
  alt: boolean;
  shift: boolean;
}

export const NO_MODIFIERS: Modifiers = { ctrl: false, alt: false, shift: false };

export interface EncodeOptions {
  /** DECCKM (application cursor keys) is set — arrows/Home/End use SS3. */
  applicationCursor: boolean;
}

/** xterm modifier parameter: 1 + Shift(1) + Alt(2) + Ctrl(4). */
function modifierParam(mods: Modifiers): number {
  return 1 + (mods.shift ? 1 : 0) + (mods.alt ? 2 : 0) + (mods.ctrl ? 4 : 0);
}

const CURSOR_FINAL: Record<"up" | "down" | "right" | "left" | "home" | "end", string> = {
  up: "A",
  down: "B",
  right: "C",
  left: "D",
  home: "H",
  end: "F",
};

/**
 * Bytes for a tapped row key under the given (armed or locked) modifiers.
 *
 * Esc and ⇧Tab are atomic: they ignore modifiers. ⇧Tab is always the legacy
 * ``CSI Z`` — never CSI-u, which Claude Code only accepts on an allow-list of
 * terminals.
 */
export function encodeExtraKey(key: PlainKeyId, mods: Modifiers, opts: EncodeOptions): string {
  switch (key) {
    case "esc":
      return "\x1b";
    case "shift-tab":
      return "\x1b[Z";
    case "tab": {
      const base = mods.shift ? "\x1b[Z" : "\t";
      return mods.alt ? `\x1b${base}` : base;
    }
    case "up":
    case "down":
    case "left":
    case "right":
    case "home":
    case "end": {
      const final = CURSOR_FINAL[key];
      const m = modifierParam(mods);
      if (m > 1) return `\x1b[1;${m}${final}`;
      return opts.applicationCursor ? `\x1bO${final}` : `\x1b[${final}`;
    }
    case "pgup":
    case "pgdn": {
      const code = key === "pgup" ? 5 : 6;
      const m = modifierParam(mods);
      return m > 1 ? `\x1b[${code};${m}~` : `\x1b[${code}~`;
    }
    default: {
      const exhaustive: never = key;
      return exhaustive;
    }
  }
}

/** Characters whose Ctrl chord is a control byte outside the letter range. */
const CTRL_EDGE_KEYS: Record<string, string> = {
  "@": "\x00",
  " ": "\x00",
  "2": "\x00",
  "[": "\x1b",
  "3": "\x1b",
  "\\": "\x1c",
  "4": "\x1c",
  "]": "\x1d",
  "5": "\x1d",
  "^": "\x1e",
  "6": "\x1e",
  _: "\x1f",
  "7": "\x1f",
  "?": "\x7f",
  "8": "\x7f",
};

function ctrlChord(ch: string): string {
  const code = ch.charCodeAt(0);
  const isLetter = (code >= 0x41 && code <= 0x5a) || (code >= 0x61 && code <= 0x7a);
  if (isLetter) return String.fromCharCode(code & 0x1f);
  return CTRL_EDGE_KEYS[ch] ?? ch;
}

/**
 * Rewrite one soft-keyboard ``onData`` chunk under active modifiers.
 *
 * Only single-character chunks are transformable; anything longer (IME
 * commit, paste) passes through untouched. Non-transformable characters also
 * pass through unchanged — the caller still consumes armed modifiers either
 * way (Termux semantics).
 */
export function encodeModifiedInput(data: string, mods: Modifiers): string {
  if (data.length !== 1) return data;
  if (data === "\r" && mods.shift && !mods.ctrl && !mods.alt) return SHIFT_ENTER_CSI_U;
  // Shift + a soft-keyboard Tab is Shift+Tab, same as the row's own Tab key.
  const base = data === "\t" && mods.shift ? "\x1b[Z" : mods.ctrl ? ctrlChord(data) : data;
  return mods.alt ? `\x1b${base}` : base;
}

export type ModifierState = "off" | "armed" | "locked";
export type ModifierStates = Readonly<Record<ModifierId, ModifierState>>;

export const MODIFIERS_OFF: ModifierStates = { ctrl: "off", alt: "off", shift: "off" };

export type ModifierAction =
  | { type: "tap"; mod: ModifierId }
  | { type: "longPress"; mod: ModifierId }
  /** A row key or an ``onData`` chunk went out: armed → off, locked stays. */
  | { type: "consume" };

/**
 * Sticky-modifier state machine. Returns the same object when nothing
 * changes so callers can skip a React update on the hot path.
 */
export function reduceModifiers(state: ModifierStates, action: ModifierAction): ModifierStates {
  switch (action.type) {
    case "tap": {
      const next: ModifierState = state[action.mod] === "off" ? "armed" : "off";
      return { ...state, [action.mod]: next };
    }
    case "longPress":
      return state[action.mod] === "locked" ? state : { ...state, [action.mod]: "locked" };
    case "consume": {
      if (state.ctrl !== "armed" && state.alt !== "armed" && state.shift !== "armed") return state;
      return {
        ctrl: state.ctrl === "armed" ? "off" : state.ctrl,
        alt: state.alt === "armed" ? "off" : state.alt,
        shift: state.shift === "armed" ? "off" : state.shift,
      };
    }
    default: {
      const exhaustive: never = action;
      return exhaustive;
    }
  }
}

/** Armed or locked modifiers as the boolean set the encoders take. */
export function activeModifiers(state: ModifierStates): Modifiers {
  return {
    ctrl: state.ctrl !== "off",
    alt: state.alt !== "off",
    shift: state.shift !== "off",
  };
}

export function hasActiveModifier(state: ModifierStates): boolean {
  return state.ctrl !== "off" || state.alt !== "off" || state.shift !== "off";
}
