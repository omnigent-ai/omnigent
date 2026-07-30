// Preload for the find-in-page bar (electron/find/index.html) — a tiny,
// bundled, trusted page, but it gets the same contextIsolation treatment as
// everything else: a narrow contextBridge API, never raw ipcRenderer. The
// main process verifies the sender frame on every message, so this bridge
// is inert if it ever ends up attached to anything but the find bar.

"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("omnigentFind", {
  /**
   * Run / continue a search in the parent window.
   * @param {string} text The query; empty clears the current highlight.
   * @param {{forward?: boolean, findNext?: boolean}} [opts] `forward`
   *   defaults true; `findNext: true` steps through matches of the
   *   current query instead of starting a fresh search.
   */
  query: (text, opts) => {
    ipcRenderer.send("omnigent:find-query", {
      text: String(text ?? ""),
      forward: opts?.forward !== false,
      findNext: opts?.findNext === true,
    });
  },
  /** Dismiss the find bar (clears highlights, refocuses the parent). */
  close: () => {
    ipcRenderer.send("omnigent:find-close");
  },
  /**
   * Subscribe to match-count updates.
   * @param {(result: {active: number, matches: number}) => void} callback
   */
  onResult: (callback) => {
    ipcRenderer.on("omnigent:find-result", (_event, result) => callback(result));
  },
  /** Fires when Cmd/Ctrl+F re-activates an already-open bar. */
  onActivate: (callback) => {
    ipcRenderer.on("omnigent:find-activate", () => callback());
  },
});
