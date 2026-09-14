import { contextsMayOverlap } from "./context";
import { keybindingEnvironmentExpression } from "./keybindingEnvironment";
import type { ActionId, KeyStroke, KeybindingRule } from "./types";

export const DESKTOP_ACTION_BINDING_VERSION = 1 as const;

export const DESKTOP_MENU_ACTIONS = [
  "session.action.new",
  "workbench.action.navigateSettings",
  "file.action.find",
] as const satisfies readonly ActionId[];

export type DesktopMenuAction = (typeof DESKTOP_MENU_ACTIONS)[number];

export interface DesktopActionBinding {
  action: DesktopMenuAction;
  accelerator: string | null;
}

export interface DesktopActionBindingSnapshot {
  version: typeof DESKTOP_ACTION_BINDING_VERSION;
  bindings: readonly DesktopActionBinding[];
}

export interface DesktopActionInvocation {
  action: DesktopMenuAction;
  requestId: string;
}

const MODIFIER_TOKENS = {
  mod: "CmdOrCtrl",
  primary: "CmdOrCtrl",
  ctrl: "Ctrl",
  meta: "Cmd",
  alt: "Alt",
  shift: "Shift",
} as const;

const LOGICAL_KEY_TOKENS: Readonly<Record<string, string>> = {
  " ": "Space",
  enter: "Enter",
  escape: "Esc",
  tab: "Tab",
  backspace: "Backspace",
  delete: "Delete",
  arrowup: "Up",
  arrowdown: "Down",
  arrowleft: "Left",
  arrowright: "Right",
  home: "Home",
  end: "End",
  pageup: "PageUp",
  pagedown: "PageDown",
  insert: "Insert",
  ",": ",",
  ".": ".",
  "/": "/",
  ";": ";",
  "'": "'",
  "[": "[",
  "]": "]",
  "\\": "\\",
  "-": "-",
  "=": "=",
  "`": "`",
};

const RESERVED_ELECTRON_ACCELERATORS = new Set([
  "CmdOrCtrl+A",
  "CmdOrCtrl+C",
  "CmdOrCtrl+V",
  "CmdOrCtrl+X",
  "CmdOrCtrl+Z",
  "CmdOrCtrl+Shift+Z",
  "CmdOrCtrl+Y",
  "CmdOrCtrl+W",
  "CmdOrCtrl+R",
  "CmdOrCtrl+Shift+R",
  "CmdOrCtrl+Q",
  "CmdOrCtrl+Shift+N",
  "CmdOrCtrl+0",
  "CmdOrCtrl+=",
  "CmdOrCtrl+Shift+=",
  "CmdOrCtrl+-",
  "Cmd+Q",
  "Cmd+H",
  "Cmd+Alt+H",
  "Cmd+M",
  "Cmd+Alt+Shift+V",
  "Ctrl+Cmd+F",
  "Cmd+Alt+I",
  "Ctrl+Shift+I",
  "Alt+F4",
  "F11",
  "F12",
]);

const CODE_KEY_TOKENS: Readonly<Record<string, string>> = {
  Space: "Space",
  Enter: "Enter",
  Escape: "Esc",
  Tab: "Tab",
  Backspace: "Backspace",
  Delete: "Delete",
  ArrowUp: "Up",
  ArrowDown: "Down",
  ArrowLeft: "Left",
  ArrowRight: "Right",
  BracketLeft: "[",
  BracketRight: "]",
  Comma: ",",
  Period: ".",
  Slash: "/",
  Semicolon: ";",
  Quote: "'",
  Backslash: "\\",
  Minus: "-",
  Equal: "=",
  Backquote: "`",
};

function acceleratorKey(stroke: KeyStroke): string | null {
  const { key } = stroke;
  if (key.kind === "code") {
    const mapped = CODE_KEY_TOKENS[key.value];
    if (mapped) return mapped;
    const match = /^(?:Key([A-Z])|Digit([0-9])|F([1-9]|1[0-9]|2[0-4]))$/.exec(key.value);
    return match ? (match[1] ?? match[2] ?? `F${match[3]}`) : null;
  }
  const mapped = LOGICAL_KEY_TOKENS[key.value.toLowerCase()];
  if (mapped) return mapped;
  if (/^[a-z0-9]$/i.test(key.value)) return key.value.toUpperCase();
  if (/^F(?:[1-9]|1[0-9]|2[0-4])$/i.test(key.value)) return key.value.toUpperCase();
  return null;
}

/** Convert one web key stroke to Electron's menu accelerator syntax. */
export function keyStrokeToElectronAccelerator(stroke: KeyStroke): string | null {
  const key = acceleratorKey(stroke);
  if (!key) return null;
  // Menu accelerators are app-global in Electron. Never turn a plain or
  // Shift-only renderer binding into a key that intercepts normal input.
  if (!stroke.modifiers.some((modifier) => modifier !== "shift")) return null;
  const modifiers = stroke.modifiers.map((modifier) => MODIFIER_TOKENS[modifier]);
  const accelerator = [...new Set(modifiers), key].join("+");
  return RESERVED_ELECTRON_ACCELERATORS.has(accelerator) ? null : accelerator;
}

/** Build the complete menu-owned binding snapshot for an Electron window. */
export function desktopActionBindingSnapshot(
  rules: readonly KeybindingRule[],
  isMac: boolean,
): DesktopActionBindingSnapshot {
  const environment = keybindingEnvironmentExpression({
    isMac,
    isNativeShell: true,
    isEmbedded: false,
  });
  const selected = DESKTOP_MENU_ACTIONS.map((action) => {
    const candidates = rules.filter(
      (candidate) => candidate.action === action && contextsMayOverlap(candidate.when, environment),
    );
    const rule = candidates.find((candidate) => candidate.origin === "user") ?? candidates[0];
    return {
      action,
      accelerator: rule ? keyStrokeToElectronAccelerator(rule.sequence[0]) : null,
    };
  });
  const counts = new Map<string, number>();
  for (const { accelerator } of selected) {
    if (accelerator) counts.set(accelerator, (counts.get(accelerator) ?? 0) + 1);
  }
  return {
    version: DESKTOP_ACTION_BINDING_VERSION,
    // Native menus cannot contextually resolve duplicate accelerators. Leave
    // collisions to the renderer dispatcher, which has scope information.
    bindings: selected.map((binding) => ({
      ...binding,
      accelerator:
        binding.accelerator && counts.get(binding.accelerator) === 1 ? binding.accelerator : null,
    })),
  };
}

export function isDesktopMenuAction(action: string): action is DesktopMenuAction {
  return (DESKTOP_MENU_ACTIONS as readonly string[]).includes(action);
}
