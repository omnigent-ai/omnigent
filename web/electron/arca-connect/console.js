// Renderer logic for the Arca connect console (see index.html for the page
// and src/arca_connect_window.js for the flow + trust model). Talks only to
// the narrow `window.arcaConnect` bridge exposed by arca_connect_preload.js.
"use strict";

const serverEl = document.getElementById("server");
const titleEl = document.getElementById("title");
const introEl = document.getElementById("intro");
const commandEl = document.getElementById("command");
const terminalEl = document.getElementById("terminal");
const statusEl = document.getElementById("status");
const statusMessageEl = document.getElementById("status-message");
const statusActionEl = document.getElementById("status-action");
const diagnosticsEl = document.getElementById("diagnostics");
const copyDiagnosticsBtn = document.getElementById("copy-diagnostics");
const copyStateEl = document.getElementById("copy-state");
const confirmBtn = document.getElementById("confirm");
const cancelBtn = document.getElementById("cancel");

let resizeFrame = null;
function resizeToContent() {
  if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(() => {
    resizeFrame = null;
    window.arcaConnect.resizeToContent({
      height: document.documentElement.scrollHeight,
      phase: document.body.dataset.phase,
    });
  });
}

void document.fonts?.ready.then(resizeToContent).catch(() => {});

window.arcaConnect.onInit(({ serverUrl, command, reason }) => {
  // textContent only: the server URL is remote-influenced data.
  serverEl.textContent = serverUrl;
  commandEl.textContent = command;
  const reconnect = reason === "preview-reconnect";
  titleEl.textContent = reconnect
    ? "Reconnect Arca for this preview?"
    : "Connect your Arca instance?";
  introEl.textContent = reconnect
    ? "To load this preview, run the command below over "
    : "This runs the command below over ";
  confirmBtn.textContent = reconnect ? "Reconnect" : "Connect";
  resizeToContent();
});

confirmBtn.addEventListener("click", () => {
  window.arcaConnect.confirm();
});
// Cancel doubles as Close after the run; the main process treats both as
// "close the window" and settles accordingly.
cancelBtn.addEventListener("click", () => {
  window.arcaConnect.cancel();
});

window.arcaConnect.onStarted(({ login, command }) => {
  commandEl.textContent = command;
  // Reveal the terminal pane (the main process grows the window to match).
  document.body.dataset.phase = "running";
  resizeToContent();
  // Mount the terminal now that its container has real dimensions.
  requestAnimationFrame(() => ensureTerminal());
  // The button itself carries the progress: spinner + "Connecting…".
  confirmBtn.disabled = true;
  confirmBtn.classList.add("loading");
  confirmBtn.textContent = login ? "Signing in…" : "Connecting…";
  statusEl.dataset.kind = "";
  statusActionEl.textContent = "";
  diagnosticsEl.hidden = true;
  statusMessageEl.textContent = login
    ? "Complete sign-in in your browser. If no browser opened, check that Arca Companion is running. Credentials stay on the Arca instance."
    : "A cold instance can take a few minutes to start.";
});

// The output pane is a real terminal (xterm.js, read-only), so arca/ssh ANSI
// output — colors, \r progress rewrites, cursor movement — renders exactly as
// it would in a shell. Loaded from the packaged node_modules (UMD globals:
// `Terminal`, `FitAddon`); created lazily on first output since the pane is
// hidden (zero-sized) until the run starts.
let term = null;
let fitAddon = null;

function ensureTerminal() {
  if (term !== null) return term;
  term = new window.Terminal({
    convertEol: true, // ssh output is \n-terminated
    disableStdin: true,
    fontSize: 12,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    theme: { background: "#0d1117", foreground: "#e6edf3" },
  });
  fitAddon = new window.FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(terminalEl);
  fitAddon.fit();
  window.addEventListener("resize", () => fitAddon.fit());
  return term;
}

window.arcaConnect.onOutput((text) => {
  ensureTerminal().write(text);
});

window.arcaConnect.onDone(({ ok, error, errorMessage, errorAction, canCopyDiagnostics }) => {
  statusEl.dataset.kind = ok ? "ok" : "error";
  // A structured failure puts the problem and the next step on separate lines;
  // a legacy combined `error` stays on one line.
  statusMessageEl.textContent = ok
    ? "Connected. The Arca host should appear online shortly."
    : (errorMessage ?? error);
  statusActionEl.textContent = !ok && errorMessage && errorAction ? errorAction : "";
  diagnosticsEl.hidden = ok || canCopyDiagnostics !== true;
  copyStateEl.textContent = "";
  confirmBtn.classList.remove("loading");
  confirmBtn.hidden = true;
  cancelBtn.textContent = "Close";
  resizeToContent();
  // The result rows shrink the terminal pane; refit so it doesn't overlap them.
  requestAnimationFrame(() => fitAddon?.fit());
});

// The shell builds the text from its own record of the failure and reports
// whether the clipboard write actually succeeded.
copyDiagnosticsBtn.addEventListener("click", async () => {
  copyDiagnosticsBtn.disabled = true;
  copyStateEl.textContent = "";
  const copied = await Promise.resolve()
    .then(() => window.arcaConnect.copyDiagnostics())
    .then(
      (result) => result?.ok === true,
      () => false,
    );
  copyStateEl.textContent = copied ? "Copied" : "Couldn't copy. Try again.";
  copyDiagnosticsBtn.disabled = false;
});
