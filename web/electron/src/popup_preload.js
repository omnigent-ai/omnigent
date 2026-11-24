// Deliberately empty preload for OAuth popups and design-tool overlays: a window.open child
// may inherit the shell's preload.js, whose omnigentDesktop/omnigentSetup bridges must never
// reach it; pointing the child here guarantees no bridge.

"use strict";
