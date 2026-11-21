// Focused surfaces that own their keystrokes, built the way the app mounts them.

/** A terminal, the Monaco code editor, or the CodeMirror file editor. */
export type KeystrokeSurface = "xterm" | "monaco-editor" | "cm-editor";

/** Every surface whose keystrokes app hotkeys must leave alone. */
export const KEYSTROKE_SURFACES = [
  "xterm",
  "monaco-editor",
  "cm-editor",
] as const satisfies KeystrokeSurface[];

/** Append `kind` to the document, focus its text input, and return both. */
export function mountKeystrokeSurface(kind: KeystrokeSurface): {
  surface: HTMLElement;
  input: HTMLTextAreaElement;
} {
  const surface = document.createElement("div");
  const input = document.createElement("textarea");
  if (kind === "xterm") {
    surface.className = "xterm";
    input.className = "xterm-helper-textarea";
  } else {
    surface.className = kind;
  }
  surface.append(input);
  document.body.append(surface);
  input.focus();
  return { surface, input };
}
