import type { KeyModifier, KeySequence, KeyStroke } from "./types";

const MODIFIER_ORDER: readonly KeyModifier[] = ["mod", "primary", "ctrl", "meta", "alt", "shift"];
const MODIFIER_ALIASES: Readonly<Record<string, KeyModifier>> = {
  mod: "mod",
  cmdorctrl: "mod",
  commandorcontrol: "mod",
  ctrlcmd: "mod",
  primary: "primary",
  eithermod: "primary",
  ctrl: "ctrl",
  control: "ctrl",
  meta: "meta",
  cmd: "meta",
  command: "meta",
  alt: "alt",
  option: "alt",
  shift: "shift",
};

const NAMED_KEYS: Readonly<Record<string, string>> = {
  enter: "Enter",
  return: "Enter",
  escape: "Escape",
  esc: "Escape",
  tab: "Tab",
  backspace: "Backspace",
  delete: "Delete",
  del: "Delete",
  insert: "Insert",
  space: " ",
  spacebar: " ",
  plus: "+",
  arrowup: "ArrowUp",
  up: "ArrowUp",
  arrowdown: "ArrowDown",
  down: "ArrowDown",
  arrowleft: "ArrowLeft",
  left: "ArrowLeft",
  arrowright: "ArrowRight",
  right: "ArrowRight",
  home: "Home",
  end: "End",
  pageup: "PageUp",
  pagedown: "PageDown",
};

const NAMED_CODES = new Set([
  "Backquote",
  "Minus",
  "Equal",
  "BracketLeft",
  "BracketRight",
  "Backslash",
  "Semicolon",
  "Quote",
  "Comma",
  "Period",
  "Slash",
  "Enter",
  "Tab",
  "Space",
  "Backspace",
  "Delete",
  "Insert",
  "Home",
  "End",
  "PageUp",
  "PageDown",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "Escape",
  "NumpadAdd",
  "NumpadSubtract",
  "NumpadMultiply",
  "NumpadDivide",
  "NumpadDecimal",
  "NumpadEnter",
]);

function isKnownPhysicalCode(value: string): boolean {
  return (
    NAMED_CODES.has(value) ||
    /^Key[A-Z]$/.test(value) ||
    /^Digit[0-9]$/.test(value) ||
    /^Numpad[0-9]$/.test(value) ||
    /^F(?:[1-9]|1\d|2[0-4])$/.test(value)
  );
}

function normalizeLogicalKey(value: string): string {
  const named = NAMED_KEYS[value.toLowerCase()];
  if (named !== undefined) return named;
  if (/^f(?:[1-9]|1\d|2[0-4])$/i.test(value)) return value.toUpperCase();
  if ([...value].length === 1) return value.toLocaleLowerCase();
  throw new Error(`Unknown logical key: ${value}`);
}

function validateModifierCombination(modifiers: ReadonlySet<KeyModifier>, source: string): void {
  if (modifiers.has("primary") && modifiers.size > 1) {
    const conflicts = ["mod", "ctrl", "meta"].some((modifier) =>
      modifiers.has(modifier as KeyModifier),
    );
    if (conflicts)
      throw new Error(`primary cannot be combined with another primary modifier: ${source}`);
  }
  if (
    modifiers.has("mod") &&
    (modifiers.has("primary") || modifiers.has("ctrl") || modifiers.has("meta"))
  ) {
    throw new Error(`mod cannot be combined with ctrl, meta, or primary: ${source}`);
  }
}

function parseStroke(source: string): KeyStroke {
  const parts = source.split("+");
  if (parts.some((part) => part === "")) throw new Error(`Invalid key stroke: ${source}`);

  const modifiers = new Set<KeyModifier>();
  let key: KeyStroke["key"] | undefined;
  for (const rawPart of parts) {
    const part = rawPart.trim();
    const modifier = MODIFIER_ALIASES[part.toLowerCase()];
    if (modifier) {
      if (key) throw new Error(`Modifier must precede the key in: ${source}`);
      if (modifiers.has(modifier)) throw new Error(`Duplicate modifier in: ${source}`);
      modifiers.add(modifier);
      continue;
    }

    if (key) throw new Error(`Key stroke has multiple keys: ${source}`);
    if (part.startsWith("[") || part.endsWith("]")) {
      if (!/^\[[A-Za-z][A-Za-z0-9]*\]$/.test(part)) {
        throw new Error(`Invalid physical key code in: ${source}`);
      }
      const code = part.slice(1, -1);
      if (!isKnownPhysicalCode(code)) throw new Error(`Unknown physical key code: ${code}`);
      key = { kind: "code", value: code };
    } else {
      key = { kind: "key", value: normalizeLogicalKey(part) };
    }
  }

  if (!key) throw new Error(`Key stroke is missing a key: ${source}`);
  validateModifierCombination(modifiers, source);
  return {
    modifiers: MODIFIER_ORDER.filter((modifier) => modifiers.has(modifier)),
    key,
  };
}

/** Parse one key combination. Multi-stroke chords are intentionally unsupported. */
export function parseKeybinding(source: string): KeySequence {
  const trimmed = source.trim();
  if (!trimmed) throw new Error("Keybinding cannot be empty");
  const strokes = trimmed.split(/\s+/).map(parseStroke);
  if (strokes.length !== 1) throw new Error("Keybindings support one key combination");
  return [strokes[0]!];
}

function serializeLogicalKey(value: string): string {
  if (value === " ") return "space";
  if (value === "+") return "plus";
  if ([...value].length === 1) return value.toLocaleLowerCase();
  return value;
}

export function serializeKeybinding(sequence: KeySequence): string {
  return sequence
    .map((stroke) => {
      const modifierSet = new Set(stroke.modifiers);
      if (modifierSet.size !== stroke.modifiers.length) {
        throw new Error("Key stroke contains duplicate modifiers");
      }
      validateModifierCombination(modifierSet, "key stroke");
      const modifiers = MODIFIER_ORDER.filter((modifier) => modifierSet.has(modifier));
      const key =
        stroke.key.kind === "code"
          ? `[${stroke.key.value}]`
          : serializeLogicalKey(stroke.key.value);
      if (stroke.key.kind === "code" && !isKnownPhysicalCode(stroke.key.value)) {
        throw new Error(`Unknown physical key code: ${stroke.key.value}`);
      }
      return [...modifiers, key].join("+");
    })
    .join(" ");
}
