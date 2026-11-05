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
const confirmBtn = document.getElementById("confirm");
const cancelBtn = document.getElementById("cancel");
const hideBtn = document.getElementById("hide");
const trustNoticeEl = document.getElementById("trust-notice");
const backgroundNoticeEl = document.getElementById("background-notice");

// "consent" asks before anything runs; "output" follows a confirmed run.
let view = "consent";

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

window.arcaConnect.onInit(({ serverUrl, command, reason, view: initView }) => {
  // textContent only: the server URL is remote-influenced data.
  serverEl.textContent = serverUrl;
  commandEl.textContent = command;
  view = initView === "output" ? "output" : "consent";
  document.body.dataset.view = view;
  const reconnect = reason === "preview-reconnect";
  if (view === "output") {
    titleEl.textContent = reconnect ? "Reconnecting Arca" : "Connecting your Arca instance";
    introEl.textContent = "Running the command below over ";
    trustNoticeEl.hidden = true;
    backgroundNoticeEl.hidden = false;
    confirmBtn.hidden = true;
    hideBtn.hidden = false;
    cancelBtn.textContent = "Stop connecting";
  } else {
    titleEl.textContent = reconnect
      ? "Reconnect Arca for this preview?"
      : "Connect your Arca instance?";
    introEl.textContent = reconnect
      ? "To load this preview, run the command below over "
      : "This runs the command below over ";
    confirmBtn.textContent = reconnect ? "Reconnect" : "Connect";
  }
  resizeToContent();
});

confirmBtn.addEventListener("click", () => {
  window.arcaConnect.confirm();
});
// Cancel declines consent, and Close dismisses a finished run; both just close
// the window. In the output view of a running connect it stops the command.
cancelBtn.addEventListener("click", () => {
  if (view === "output" && document.body.dataset.phase === "running") {
    window.arcaConnect.stop();
  } else {
    window.arcaConnect.cancel();
  }
});
// Hide closes the output window; the connection keeps running.
hideBtn.addEventListener("click", () => {
  window.arcaConnect.cancel();
});

window.arcaConnect.onStarted(({ login, command }) => {
  commandEl.textContent = command;
  // Reveal the terminal pane (the main process grows the window to match).
  document.body.dataset.phase = "running";
  resizeToContent();
  // Mount the terminal now that its container has real dimensions.
  requestAnimationFrame(() => ensureTerminal());
  // Only the output view receives started; it has no Connect button.
  confirmBtn.hidden = true;
  statusEl.dataset.kind = "";
  statusEl.textContent = login
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

window.arcaConnect.onDone(({ ok, error }) => {
  statusEl.dataset.kind = ok ? "ok" : "error";
  statusEl.textContent = ok ? "Connected. The Arca host should appear online shortly." : error;
  document.body.dataset.phase = "done";
  confirmBtn.hidden = true;
  hideBtn.hidden = true;
  cancelBtn.textContent = "Close";
  resizeToContent();
});
