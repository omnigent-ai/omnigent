"use strict";

const { randomUUID } = require("node:crypto");

/** Sent to a main window's page with each offer; the page takes it, then confirms it. */
const FLOATING_LAUNCH_OFFER_CHANNEL = "omnigent:floating-composer:launch-offer";
/** How long an offer waits to be taken and confirmed before the panel sends the message. */
const FLOATING_LAUNCH_TAKE_TIMEOUT_MS = 2000;

/**
 * The panel's launch offers to main windows. A page takes an offer to get its message, then
 * confirms once it has queued the message. Only a confirmed offer is the page's to send; every
 * other ending leaves delivery to the panel. Each offer settles once, so a confirm, an expiry and
 * a document going can't all win.
 *
 * @param {{
 *   timeoutMs?: number,
 *   startTimer?: (callback: () => void, ms: number) => unknown,
 *   stopTimer?: (timer: unknown) => void,
 * }} [options]
 */
/** How an offer ends: by whether it was taken, and whether the document that took it has gone. */
function endingOutcome(offer, documentGone) {
  if (!offer.handedOut) return "not-taken";
  return documentGone ? "document-gone" : "handed-out";
}

function createFloatingLaunchOffers({
  timeoutMs = FLOATING_LAUNCH_TAKE_TIMEOUT_MS,
  startTimer = (callback, ms) => setTimeout(callback, ms),
  stopTimer = (timer) => clearTimeout(timer),
} = {}) {
  /** @type {Set<object>} webContents whose current document takes launches */
  const takers = new Set();
  /** @type {Map<string, { contents: object, message: object, handedOut: boolean, settle: (outcome: string) => void }>} */
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
    /**
     * The page stops taking offers, or its document is gone (`documentGone`), and its pending
     * offers end. An offer whose message was taken ends as "handed-out" while the page is still
     * there, and as "document-gone" once the document that took it has gone.
     */
    forget(contents, { documentGone = false } = {}) {
      takers.delete(contents);
      for (const offer of [...outstanding.values()]) {
        if (offer.contents === contents) offer.settle(endingOutcome(offer, documentGone));
      }
    },
    /**
     * Offers `message` to the top document of `contents`. Resolves "confirmed" when that page
     * confirms it queued the message; "handed-out" when it took the message and ended unconfirmed
     * with its document still there; "document-gone" when that document went after taking it; or
     * "not-taken" when it never took it.
     *
     * @param {object} contents
     * @param {{ sessionId: string, path: string }} target
     * @param {object} message
     * @returns {Promise<"confirmed" | "handed-out" | "document-gone" | "not-taken">}
     */
    offer(contents, { sessionId, path }, message) {
      return new Promise((resolve) => {
        const id = randomUUID();
        let timer;
        const settle = (outcome) => {
          if (!outstanding.delete(id)) return;
          stopTimer(timer);
          resolve(outcome);
        };
        const offer = { contents, message, handedOut: false, settle };
        outstanding.set(id, offer);
        timer = startTimer(() => settle(endingOutcome(offer, false)), timeoutMs);
        try {
          contents.send(FLOATING_LAUNCH_OFFER_CHANNEL, { id, sessionId, path });
        } catch {
          settle("not-taken");
        }
      });
    },
    /**
     * The page takes the offer it was sent, once. Resolves its message, and only to the document
     * it was offered to; null for anything else. Taking doesn't settle the offer: the page must
     * still confirm.
     *
     * @param {object} contents
     * @param {string} id
     * @returns {object | null}
     */
    take(contents, id) {
      const offer = outstanding.get(id);
      if (offer === undefined || offer.contents !== contents || offer.handedOut) return null;
      offer.handedOut = true;
      return offer.message;
    },
    /**
     * The page has queued the message it took. Settles the offer "confirmed" and answers true,
     * only for the document it was offered to, after a take, and before the offer ended.
     *
     * @param {object} contents
     * @param {string} id
     * @returns {boolean}
     */
    confirm(contents, id) {
      const offer = outstanding.get(id);
      if (offer === undefined || offer.contents !== contents || !offer.handedOut) return false;
      offer.settle("confirmed");
      return true;
    },
  };
}

module.exports = {
  FLOATING_LAUNCH_OFFER_CHANNEL,
  FLOATING_LAUNCH_TAKE_TIMEOUT_MS,
  createFloatingLaunchOffers,
};
