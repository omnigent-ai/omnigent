"use strict";

const { randomUUID } = require("node:crypto");

/** Sent to a main window's page with each offer; the page answers with `take-launch`. */
const FLOATING_LAUNCH_OFFER_CHANNEL = "omnigent:floating-composer:launch-offer";
/** How long an offer waits for its page to take the first message before the panel sends it. */
const FLOATING_LAUNCH_TAKE_TIMEOUT_MS = 2000;

/**
 * The panel's launch offers to main windows. A page that takes one gets its message; an offer
 * the page doesn't take, or whose document goes first, ends with the panel delivering it. Each
 * offer settles once, so a take and an expiry can't both win.
 *
 * @param {{
 *   timeoutMs?: number,
 *   startTimer?: (callback: () => void, ms: number) => unknown,
 *   stopTimer?: (timer: unknown) => void,
 * }} [options]
 */
function createFloatingLaunchOffers({
  timeoutMs = FLOATING_LAUNCH_TAKE_TIMEOUT_MS,
  startTimer = (callback, ms) => setTimeout(callback, ms),
  stopTimer = (timer) => clearTimeout(timer),
} = {}) {
  /** @type {Set<object>} webContents whose current document takes launches */
  const takers = new Set();
  /** @type {Map<string, { contents: object, message: object, end: (taken: boolean) => void }>} */
  const outstanding = new Map();

  return {
    /** The page subscribed for offers; the caller has checked its document supports the panel. */
    registerTaker(contents) {
      takers.add(contents);
    },
    /** @returns {boolean} */
    canOffer(contents) {
      return takers.has(contents);
    },
    /** The document was replaced or closed: it takes no more offers, and its pending ones end. */
    forget(contents) {
      takers.delete(contents);
      for (const offer of [...outstanding.values()]) {
        if (offer.contents === contents) offer.end(false);
      }
    },
    /**
     * Offers `message` to the top document of `contents`. Resolves true when that page takes it,
     * false when it doesn't in time, its document goes, or it can't be reached.
     *
     * @param {object} contents
     * @param {{ sessionId: string, path: string }} target
     * @param {object} message
     * @returns {Promise<boolean>}
     */
    offer(contents, { sessionId, path }, message) {
      return new Promise((resolve) => {
        const id = randomUUID();
        let timer;
        const end = (taken) => {
          if (!outstanding.delete(id)) return;
          stopTimer(timer);
          resolve(taken);
        };
        outstanding.set(id, { contents, message, end });
        timer = startTimer(() => end(false), timeoutMs);
        try {
          contents.send(FLOATING_LAUNCH_OFFER_CHANNEL, { id, sessionId, path });
        } catch {
          end(false);
        }
      });
    },
    /**
     * The page takes the offer it was sent. Resolves its message once, and only to the document
     * it was offered to; null for anything else, including an offer that already expired.
     *
     * @param {object} contents
     * @param {string} id
     * @returns {object | null}
     */
    take(contents, id) {
      const offer = outstanding.get(id);
      if (offer === undefined || offer.contents !== contents) return null;
      offer.end(true);
      return offer.message;
    },
  };
}

module.exports = {
  FLOATING_LAUNCH_OFFER_CHANNEL,
  FLOATING_LAUNCH_TAKE_TIMEOUT_MS,
  createFloatingLaunchOffers,
};
