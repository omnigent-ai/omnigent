// Screenshot capture for the embedded browser pane.
//
// Why: browserViewRegistry keeps only the ACTIVE conversation's WebContentsView
// attached to the window. On Electron 42, `capturePage()` on a view that was
// attached and then detached NEVER resolves (the agent saw a 30 s "browser
// action timed out"), and on a view that was never attached it resolves with an
// empty 0x0 image. So the agent's `browser_screenshot` failed whenever the
// user was looking at another chat.
//
// Fix: stage a detached view at the bottom of the window's child views with
// only its top-left pixel inside the window (invisible to the user, but the
// compositor still paints it), capture, then put it back exactly as it was.
// A never-painted view is empty for the first ~50 ms, so we retry briefly.

"use strict";

/** @param {number} ms */
const defaultWait = (ms) =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

const EMPTY_ERROR = "the browser view has not painted yet; retry in a moment";

/** @param {{ isEmpty?: () => boolean, getSize?: () => {width:number,height:number} } | null} image */
function isEmptyImage(image) {
  if (!image) return true;
  try {
    if (typeof image.isEmpty === "function" && image.isEmpty()) return true;
    if (typeof image.getSize === "function") {
      const s = image.getSize();
      if (!s || !s.width || !s.height) return true;
    }
  } catch {
    return true;
  }
  return false;
}

/** Race one capturePage() call against a timeout. Throws a tagged error on timeout. */
function capturePageWithTimeout(view, timeoutMs, budgetMs = timeoutMs) {
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      const err = new Error(
        `screenshot timed out after ${Math.round(budgetMs / 1000)} s: the browser view did not paint. ` +
          "Open this chat's browser pane in the Omnigent desktop app and retry.",
      );
      err.timedOut = true;
      reject(err);
    }, timeoutMs);
  });
  return Promise.race([view.webContents.capturePage(), timeout]).finally(() => clearTimeout(timer));
}

function isAttached(win, view) {
  if (!win) return true; // cannot tell; keep the old behavior (plus the timeout)
  try {
    return win.contentView.children.includes(view);
  } catch {
    return true;
  }
}

function contentSize(win) {
  if (typeof win.getContentBounds === "function") {
    const b = win.getContentBounds();
    if (b && b.width && b.height) return { width: b.width, height: b.height };
  }
  if (typeof win.getContentSize === "function") {
    const [width, height] = win.getContentSize();
    return { width, height };
  }
  return { width: 0, height: 0 };
}

const CLOSED_ERROR = "this chat's browser view was closed";

async function captureOnce({
  registry,
  conversationId,
  entry,
  win,
  timeoutMs,
  retryMs,
  maxWaitMs,
  wait,
}) {
  // A capture queued behind another may start after the chat's view was closed
  // (or closed and reopened as a new entry); never stage a dead view.
  const isLive = () => registry.get(conversationId) === entry;
  if (!isLive()) return { ok: false, error: CLOSED_ERROR };
  const view = entry.view;
  const toResult = (image) => ({
    ok: true,
    dataUrl: `data:image/png;base64,${image.toPNG().toString("base64")}`,
  });

  if (isAttached(win, view)) {
    const image = await capturePageWithTimeout(view, timeoutMs);
    return isEmptyImage(image) ? { ok: false, error: EMPTY_ERROR } : toResult(image);
  }

  const savedBounds = view.getBounds();
  const savedVisible = typeof view.getVisible === "function" ? view.getVisible() : true;
  const winSize = contentSize(win);
  const width = savedBounds && savedBounds.width ? savedBounds.width : winSize.width;
  const height = savedBounds && savedBounds.height ? savedBounds.height : winSize.height;
  try {
    view.setBounds({ x: winSize.width - 1, y: winSize.height - 1, width, height });
    if (!savedVisible) view.setVisible(true);
    win.contentView.addChildView(view, 0);

    // One clock for the whole capture: retries start only within maxWaitMs and
    // share timeoutMs, so slow empty paints cannot keep the view staged for minutes.
    const start = Date.now();
    const remaining = () => Math.max(1, timeoutMs - (Date.now() - start));
    let image = await capturePageWithTimeout(view, remaining(), timeoutMs);
    while (isEmptyImage(image) && Date.now() - start < maxWaitMs) {
      // Sequential on purpose: each retry follows the previous paint attempt.
      // oxlint-disable-next-line no-await-in-loop
      await wait(retryMs);
      // oxlint-disable-next-line no-await-in-loop
      image = await capturePageWithTimeout(view, remaining(), timeoutMs);
    }
    return isEmptyImage(image) ? { ok: false, error: EMPTY_ERROR } : toResult(image);
  } finally {
    if (!isLive() || registry.activeConversationId() !== conversationId) {
      try {
        win.contentView.removeChildView(view);
      } catch {
        /* window or view already gone */
      }
      try {
        view.setBounds(savedBounds);
        if (!savedVisible) view.setVisible(false);
      } catch {
        /* view destroyed */
      }
    } else {
      // The user switched to this chat mid-capture; setActive re-attached it.
      // Undo the staging position first: resync applies nothing until the pane
      // has sent bounds.
      try {
        view.setBounds(savedBounds);
        entry.boundsController.resync();
      } catch {
        /* view destroyed */
      }
    }
  }
}

/**
 * Capture a conversation's browser view as a PNG data URL, even when the view
 * is detached because another chat is active. Never throws.
 *
 * @returns {Promise<{ ok: true, dataUrl: string } | { ok: false, error: string }>}
 */
function captureBrowserView({
  registry,
  conversationId,
  entry,
  win,
  timeoutMs = 10000,
  retryMs = 50,
  maxWaitMs = 1000,
  wait = defaultWait,
}) {
  const run = () =>
    captureOnce({
      registry,
      conversationId,
      entry,
      win,
      timeoutMs,
      retryMs,
      maxWaitMs,
      wait,
    }).catch((e) => ({ ok: false, error: e && e.message ? e.message : String(e) }));
  // One capture at a time per view so two screenshots cannot stage it twice.
  const prev = entry.captureChain || Promise.resolve();
  const next = prev.then(run, run);
  entry.captureChain = next;
  return next;
}

module.exports = { captureBrowserView };
