// Renderer logic for the SSH connect console (see index.html for the page and
// src/ssh_connect_window.js for the flow and trust model). Talks only to the
// narrow `window.sshConnect` bridge from ssh_connect_preload.js.
"use strict";

const destinationTitleEl = document.getElementById("destination-title");
const destinationEl = document.getElementById("destination");
const serverEl = document.getElementById("server");
const commandsEl = document.getElementById("commands");
const terminalEl = document.getElementById("terminal");
const statusEl = document.getElementById("status");
const confirmBtn = document.getElementById("confirm");
const cancelBtn = document.getElementById("cancel");
const promptEl = document.getElementById("prompt");
const promptTitleEl = document.getElementById("prompt-title");
const promptTextEl = document.getElementById("prompt-text");
const promptInput = document.getElementById("prompt-input");
const promptOk = document.getElementById("prompt-ok");
const promptCancel = document.getElementById("prompt-cancel");
const footerEl = document.querySelector("footer");

let destination = "this machine";
let resizeFrame = null;
function resizeToContent() {
  if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(() => {
    resizeFrame = null;
    // While running, the body is viewport-tall, so overflow past it eats the
    // bottom padding instead of growing scrollHeight; measure to the footer.
    const footerBottom =
      footerEl.getBoundingClientRect().bottom +
      window.scrollY +
      parseFloat(getComputedStyle(document.body).paddingBottom || "0");
    window.sshConnect.resizeToContent({
      height: Math.max(document.documentElement.scrollHeight, Math.ceil(footerBottom)),
      phase: document.body.dataset.phase,
    });
  });
}
void document.fonts?.ready.then(resizeToContent).catch(() => {});

window.sshConnect.onInit((init) => {
  destination = init.destination;
  destinationTitleEl.textContent = init.destination;
  destinationEl.textContent = init.destination;
  serverEl.textContent = init.serverUrl;
  commandsEl.replaceChildren(
    ...init.commands.map((command) => {
      const line = document.createElement("div");
      line.textContent = command;
      return line;
    }),
  );
  resizeToContent();
});

confirmBtn.addEventListener("click", () => window.sshConnect.confirm());
// Cancel doubles as Close after the run; main treats both as "close the window".
cancelBtn.addEventListener("click", () => window.sshConnect.cancel());

let term = null;
let fitAddon = null;
function ensureTerminal() {
  if (term !== null) return term;
  term = new window.Terminal({
    convertEol: true,
    disableStdin: true,
    fontSize: 12,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    theme: { background: "#0d1117", foreground: "#e6edf3" },
  });
  fitAddon = new window.FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(terminalEl);
  fitAddon.fit();
  // The panel shrinks when a prompt or a long status appears, not only on window resize.
  new window.ResizeObserver(() => fitAddon.fit()).observe(terminalEl);
  return term;
}

window.sshConnect.onStarted(() => {
  document.body.dataset.phase = "running";
  resizeToContent();
  requestAnimationFrame(() => ensureTerminal());
  confirmBtn.disabled = true;
  confirmBtn.classList.add("loading");
  confirmBtn.textContent = "Connecting...";
  showStatus("", `Connecting to ${destination}...`);
});

const STATUS_TEXT = {
  connecting: () => `Connecting to ${destination}...`,
  "signing-in": () => `Signing in to the server on ${destination}...`,
  "waiting-for-browser": () =>
    "Finish signing in in your browser. This window updates when you're done.",
  "confirm-code": (status) => `Confirm this code in your browser: ${status.code}`,
  installing: () => `Installing Omnigent on ${destination}. This can take a few minutes.`,
  "starting-host": () => `Starting the Omnigent host on ${destination}...`,
};

function showStatus(kind, text) {
  statusEl.dataset.kind = kind;
  statusEl.textContent = text;
  resizeToContent();
}

window.sshConnect.onStatus((status) => {
  const text = STATUS_TEXT[status?.step];
  if (text) showStatus("", text(status));
});

window.sshConnect.onOutput((text) => {
  ensureTerminal().write(text);
});

// One prompt at a time: ssh asks sequentially.
let activePromptId = null;
const PROMPT_COPY = {
  confirm: { title: "Trust this machine?", ok: "Trust", input: null },
  secret: { title: "SSH asks for a secret", ok: "Continue", input: "password" },
  text: { title: "SSH asks a question", ok: "Continue", input: "text" },
  install: { title: "Install Omnigent?", ok: "Install", input: null },
};

window.sshConnect.onPrompt(({ id, kind, text }) => {
  const copy = PROMPT_COPY[kind] ?? PROMPT_COPY.text;
  activePromptId = id;
  promptEl.dataset.kind = kind;
  promptTitleEl.textContent = copy.title;
  promptTextEl.textContent = text;
  promptOk.textContent = copy.ok;
  promptInput.value = "";
  promptInput.hidden = copy.input === null;
  if (copy.input !== null) promptInput.type = copy.input;
  promptEl.hidden = false;
  resizeToContent();
  (copy.input === null ? promptOk : promptInput).focus();
});

function finishPrompt(payload) {
  if (activePromptId === null) return;
  window.sshConnect.answer({ id: activePromptId, ...payload });
  activePromptId = null;
  promptInput.value = "";
  promptEl.hidden = true;
  resizeToContent();
}

promptEl.addEventListener("submit", (event) => {
  event.preventDefault();
  const kind = promptEl.dataset.kind;
  finishPrompt({ answer: kind === "confirm" || kind === "install" ? "yes" : promptInput.value });
});
promptCancel.addEventListener("click", () => finishPrompt({ cancel: true }));
window.sshConnect.onPromptClosed(({ id }) => {
  if (id !== activePromptId) return;
  activePromptId = null;
  promptInput.value = "";
  promptEl.hidden = true;
  resizeToContent();
});
promptEl.addEventListener("keydown", (event) => {
  if (event.key === "Escape") finishPrompt({ cancel: true });
});

window.sshConnect.onDone(({ ok, error }) => {
  activePromptId = null;
  promptEl.hidden = true;
  showStatus(
    ok ? "ok" : "error",
    ok ? `Connected. ${destination} should appear in the host list shortly.` : error,
  );
  confirmBtn.classList.remove("loading");
  confirmBtn.hidden = true;
  cancelBtn.textContent = "Close";
});
