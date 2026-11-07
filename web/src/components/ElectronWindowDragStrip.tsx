// On the macOS desktop shell the native title bar is hidden ("hiddenInset"), so the
// page is the window's only drag surface. AppShell carries its own strip; pages mounted
// outside it (login, register, first-run setup, approve) render this one instead.

import { isMacElectronShell } from "@/lib/nativeBridge";

export function ElectronWindowDragStrip() {
  if (!isMacElectronShell()) return null;
  return <div className="electron-standalone-drag-strip" aria-hidden="true" />;
}
