"use strict";

const PENDING_CHANNEL = "omnigent:browser-webauthn-pending";
// Debounce so requests that settle within the delay never open a window.
const SHOW_DELAY_MS = 300;
const MAX_PROMPT_FAILURES = 3;

function pageOrigin(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.origin : null;
  } catch {
    return null;
  }
}

/** Tell the user to use their security key while the pane's page waits on WebAuthn. */
function attachSecurityKeyNotice(contents, { canPrompt, showPrompt, openExternal, cookieImport }) {
  let pending = false;
  let promptFailures = 0;
  let timer = null;
  let shown = null;

  function hide() {
    clearTimeout(timer);
    timer = null;
    const controller = shown;
    shown = null;
    controller?.abort();
  }

  function show() {
    timer = null;
    if (!pending || shown || contents.isDestroyed()) return;
    const origin = pageOrigin(contents.getURL());
    if (!origin || promptFailures >= MAX_PROMPT_FAILURES) return;
    // Keep checking so the notice appears once a hidden or covered pane comes back into view.
    if (!canPrompt(contents)) {
      timer = setTimeout(show, SHOW_DELAY_MS);
      return;
    }
    const controller = new AbortController();
    shown = controller;
    void Promise.resolve()
      .then(() =>
        showPrompt({ kind: "security-key", origin, cookieImport, signal: controller.signal }),
      )
      .then((choice) => {
        if (shown !== controller) return;
        shown = null;
        if (choice === "failed") {
          promptFailures += 1;
          if (promptFailures < MAX_PROMPT_FAILURES) {
            timer = setTimeout(show, SHOW_DELAY_MS * 2 ** (promptFailures - 1));
          }
          return;
        }
        if (choice === "suppressed") {
          timer = setTimeout(show, SHOW_DELAY_MS);
          return;
        }
        if (!pending || contents.isDestroyed()) return;
        const url = contents.getURL();
        if (choice !== "open-in-browser" || pageOrigin(url) !== origin) return;
        // The page is still waiting if the system browser fails to open, so offer the notice again.
        return Promise.resolve()
          .then(() => openExternal(url))
          .catch((error) => {
            console.warn("[browser] could not open system browser:", error);
            show();
          });
      })
      .catch(() => {
        if (shown === controller) shown = null;
      });
  }

  contents.ipc.on(PENDING_CHANNEL, (event, value) => {
    // Only top-level requests are observed; iframe requests are outside this notice's scope.
    if (event.senderFrame !== contents.mainFrame) return;
    if (!pending && value === true) promptFailures = 0;
    pending = value === true;
    if (!pending) hide();
    else if (!shown && !timer) timer = setTimeout(show, SHOW_DELAY_MS);
  });
  const reset = () => {
    pending = false;
    hide();
  };
  // The old document's request stays live until a main-frame navigation commits.
  contents.on("did-navigate", reset);
  contents.on("render-process-gone", reset);
  contents.once("destroyed", reset);
}

module.exports = { attachSecurityKeyNotice, PENDING_CHANNEL, SHOW_DELAY_MS };
