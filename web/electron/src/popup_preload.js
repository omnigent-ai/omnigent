// Deliberately empty preload for OAuth popup windows: a window.open child
// can inherit the SHELL's preload.js, whose omnigentDesktop/omnigentSetup
// IPC bridges must never reach third-party sign-in pages. Pointing the
// child here guarantees no bridge, whatever Electron's inheritance defaults.

"use strict";
