// Browser harness for manually/automatically verifying the predictive local
// echo addon in a REAL browser (Playwright drives it). It reproduces the
// TerminalSession wiring without a WebSocket: a fake PTY echoes typed bytes
// back after an artificial round-trip delay, and that echo is routed through
// `typeAhead.beforeServerInput(...)` before painting — exactly as
// TerminalSession.writeOutput does on the real path.
//
// The test controls everything via `window.__ta`.
/* eslint-disable no-underscore-dangle -- `window.__ta` is the test control API
   and `_timeline` is the addon's (intentionally) private field read for state. */

import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { TypeAheadAddon } from "../terminalTypeAheadAddon";

const RTT_MS = 250; // simulated network round-trip — well above the 30ms gate

const term = new Terminal({
  cols: 80,
  rows: 24,
  fontFamily: "monospace",
  fontSize: 14,
  theme: { background: "#131517", foreground: "#e4e4e7" },
});

// latencyThreshold 0 => "on" (show predictions immediately, no warm-up needed
// for a deterministic test). The real app uses "auto" (30ms gate).
const addon = new TypeAheadAddon({ latencyThreshold: 0, style: "dim" });
term.loadAddon(addon);
term.open(document.getElementById("term")!);

// Fake PTY: echo each byte back after RTT, through the addon's reconciler.
// A real shell prints a prompt first; emit one so the cursor isn't at column 0
// (where the addon guards the prompt edge).
function serverWrite(data: string) {
  term.write(addon.beforeServerInput(data));
}

term.onData((d) => {
  // Echo back after the simulated round-trip, like a remote PTY would.
  setTimeout(() => serverWrite(d), RTT_MS);
});

// Initial prompt (written straight, not via the reconciler — it's not an echo).
term.write("$ ");

/** Read the rendered text of a buffer row (trimmed). */
function rowText(y: number): string {
  return term.buffer.active.getLine(y)?.translateToString(true) ?? "";
}

/**
 * Read the SGR "dim" state of the cell at (x, yFromTop). xterm exposes
 * `isDim()` on the cell — predicted (unconfirmed) glyphs are painted dim.
 */
function cellIsDim(x: number, y: number): boolean {
  const cell = term.buffer.active.getLine(y)?.getCell(x);
  return cell ? cell.isDim() !== 0 : false;
}

declare global {
  interface Window {
    __ta: {
      rttMs: number;
      type: (s: string) => void;
      rowText: (y: number) => string;
      cellIsDim: (x: number, y: number) => boolean;
      cursorX: () => number;
      isShowing: () => boolean;
      ready: boolean;
    };
  }
}

window.__ta = {
  rttMs: RTT_MS,
  // `term.input` fires onData exactly as a real keystroke does.
  type: (s: string) => term.input(s),
  rowText,
  cellIsDim,
  cursorX: () => term.buffer.active.cursorX,
  isShowing: () =>
    (addon as unknown as { _timeline?: { isShowingPredictions: boolean } })._timeline
      ?.isShowingPredictions ?? false,
  ready: true,
};
