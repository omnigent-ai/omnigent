import { isIOSShell, setNativeViewMode } from "@/lib/nativeBridge";

/**
 * Retire the iOS shell's legacy bottom Chat/Terminal pill.
 *
 * The switcher lives in the header (ViewModeToggle) on every shell, so the web
 * layer never asks the native pill to show anymore. The shell's visibility
 * state persists across SPA loads inside the same WKWebView session, though —
 * a page served before this retirement may have left the pill floating — so
 * assert it hidden once at boot. Called from main.tsx before the router
 * mounts, so it covers every route (login/setup/approve included) at the
 * earliest point the SPA can act. No-op outside the iOS shell.
 *
 * Compat shim: remove together with the shell's ChatTerminalBar once iOS
 * builds without the pill are required (target 0.15).
 */
export function hideNativeChatTerminalBar(): void {
  if (!isIOSShell()) return;
  setNativeViewMode({
    mode: "chat",
    terminalEnabled: false,
    terminalStartingUp: false,
    visible: false,
  });
}
