"use strict";

const PENDING_CHANNEL = "omnigent:browser-webauthn-pending";
// Requests the page settles at once (or a page re-requesting in a loop) open no window.
const SHOW_DELAY_MS = 300;

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
    const origin = pageOrigin(contents.getURL());
    if (!pending || shown || !origin || !canPrompt(contents)) return;
    const controller = new AbortController();
    shown = controller;
    void Promise.resolve()
      .then(() =>
        showPrompt({ kind: "security-key", origin, cookieImport, signal: controller.signal }),
      )
      .then((choice) => {
        if (shown !== controller) return;
        shown = null;
        if (choice === "suppressed") return show();
        const url = contents.getURL();
        if (choice === "open-in-browser" && pageOrigin(url) === origin) openExternal(url);
      })
      .catch(() => {
        if (shown === controller) shown = null;
      });
  }

  contents.ipc.on(PENDING_CHANNEL, (event, value) => {
    if (event.senderFrame !== contents.mainFrame) return;
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
