// Preload for the find-in-page bar (electron/find/index.html) — a tiny,
// bundled, trusted page, but it gets the same contextIsolation treatment as
// everything else: a narrow contextBridge API, never raw ipcRenderer. The
// main process verifies the sender frame on every message, so this bridge
// is inert if it ever ends up attached to anything but the find bar.

import { contextBridge, ipcRenderer } from "electron";
import type { ElectronFindBridge } from "./ipc/bridge.ts";
import { createRendererIpc } from "./ipc/renderer.ts";

const ipc = createRendererIpc(ipcRenderer);

contextBridge.exposeInMainWorld("omnigentFind", {
  /**
   * Run / continue a search in the parent window.
   * @param text The query; empty clears the current highlight.
   * @param opts `forward` defaults true; `findNext: true` steps through
   *   matches of the current query instead of starting a fresh search.
   */
  query: (text, opts) => {
    ipc.send("omnigent:find-query", {
      text: String(text ?? ""),
      forward: opts?.forward !== false,
      findNext: opts?.findNext === true,
    });
  },
  /** Dismiss the find bar (clears highlights, refocuses the parent). */
  close: () => {
    ipc.send("omnigent:find-close");
  },
  /** Subscribe to match-count updates. */
  onResult: (callback) => {
    ipc.on("omnigent:find-result", (result) => callback(result));
  },
  /** Fires when Cmd/Ctrl+F re-activates an already-open bar. */
  onActivate: (callback) => {
    ipc.on("omnigent:find-activate", () => callback());
  },
} satisfies ElectronFindBridge);
