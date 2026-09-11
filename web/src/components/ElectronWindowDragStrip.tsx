// With the native title bar hidden on the macOS desktop shell (titleBarStyle
// "hiddenInset"), the web page is the window's only drag surface. AppShell
// carries its own strip; pages mounted OUTSIDE the shell (login, register,
// first-run setup, approve) render this one so the desktop window stays
// movable on those screens too. Renders nothing outside the macOS Electron
// shell — other platforms keep their native, draggable frame.

import { isMacElectronShell } from "@/lib/nativeBridge";

export function ElectronWindowDragStrip() {
  if (!isMacElectronShell()) return null;
  return <div className="electron-standalone-drag-strip" aria-hidden="true" />;
}
