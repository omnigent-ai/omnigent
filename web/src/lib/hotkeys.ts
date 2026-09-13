// Platform-aware command-modifier detection shared by every global hotkey hook.
//
// On Apple platforms the command modifier is ⌘ (metaKey); everywhere else it's
// Ctrl. Detecting the platform — instead of accepting `metaKey || ctrlKey` on
// all platforms — keeps each shortcut "what you see is what you get": the
// shortcuts dialog shows ⌘K on macOS, so only ⌘K fires there, leaving the
// Ctrl-based emacs/readline bindings (Ctrl+K, Ctrl+A, …) that users rely on in
// the composer untouched. On Windows/Linux the dialog shows Ctrl and only Ctrl
// fires.

/** True on Apple platforms (⌘ is the command modifier); false elsewhere (Ctrl). */
export function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const uaData = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData;
  const platform = uaData?.platform ?? navigator.platform ?? navigator.userAgent ?? "";
  return /Mac|iPhone|iPad|iPod/i.test(platform);
}

/**
 * True when the platform's command modifier is held *exclusively*: ⌘ (and not
 * Ctrl) on macOS, Ctrl (and not ⌘) elsewhere. Callers layer Alt/Shift on top.
 *
 * Rejecting the opposite modifier matters on macOS: Ctrl+K there is a real
 * text-editing binding (kill-to-end-of-line), so treating Ctrl as a ⌘ stand-in
 * would swallow it. `isMac` is injectable so hooks and tests can pin a platform.
 */
export function hasCommandModifier(
  e: Pick<globalThis.KeyboardEvent, "metaKey" | "ctrlKey">,
  isMac: boolean = isMacPlatform(),
): boolean {
  return isMac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
}
