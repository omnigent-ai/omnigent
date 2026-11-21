// Seed the find bar from the text selected in the page it searches, as
// browsers and editors do when Cmd/Ctrl+F opens find over a selection.

"use strict";

/** Longest selection used as a query; longer ones leave the bar unchanged. */
const MAX_FIND_SEED_LENGTH = 1000;

/** How long opening the bar waits for a busy page to report its selection. */
const FIND_SEED_TIMEOUT_MS = 300;

// Own isolated world, so page scripts can't replace the DOM APIs it reads.
const FIND_SEED_WORLD_ID = 1043;

/** Text input types whose selection may seed find (never passwords). */
const SEEDABLE_INPUT_TYPES = ["text", "search", "url", "tel", "email"];

// The focused text field's selected range, else the document selection;
// null when oversized so a whole transcript never reaches the shell.
const FIND_SELECTION_SCRIPT = `(() => {
  const active = document.activeElement;
  let text = "";
  const isControl = active instanceof HTMLTextAreaElement || active instanceof HTMLInputElement;
  const isField =
    active instanceof HTMLTextAreaElement ||
    (active instanceof HTMLInputElement &&
      ${JSON.stringify(SEEDABLE_INPUT_TYPES)}.includes(active.type));
  if (isField && active.selectionEnd > active.selectionStart) {
    text = active.value.slice(active.selectionStart, active.selectionEnd);
  } else if (!isControl) {
    // Chromium reports a focused control's selection here, so skip controls.
    text = String(window.getSelection()?.toString() ?? "");
  }
  return text.length > ${MAX_FIND_SEED_LENGTH * 4} ? null : text;
})()`;

// The query a selection seeds, or null. Multi-line text is skipped, as in
// VS Code, because find doesn't match across blocks.
function findSeedFromSelection(selection) {
  if (typeof selection !== "string") return null;
  const text = selection.trim();
  if (text === "" || /[\r\n]/.test(text) || text.length > MAX_FIND_SEED_LENGTH) return null;
  return text;
}

// The page's find seed. Resolves null when the page is gone, throws, or is
// too slow to answer, so find still opens.
async function readFindSeed(webContents, { timeoutMs = FIND_SEED_TIMEOUT_MS } = {}) {
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve(null), timeoutMs);
  });
  try {
    if (webContents.isDestroyed()) return null;
    const selection = await Promise.race([
      webContents.executeJavaScriptInIsolatedWorld(FIND_SEED_WORLD_ID, [
        { code: FIND_SELECTION_SCRIPT },
      ]),
      timeout,
    ]);
    return findSeedFromSelection(selection);
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

module.exports = {
  FIND_SEED_TIMEOUT_MS,
  FIND_SEED_WORLD_ID,
  FIND_SELECTION_SCRIPT,
  MAX_FIND_SEED_LENGTH,
  findSeedFromSelection,
  readFindSeed,
};
