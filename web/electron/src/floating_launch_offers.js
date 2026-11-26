"use strict";

// Offers a launch to its routed document; unconfirmed offers fall back to the panel.

/** How long a routed page has to take and confirm an offer before the panel delivers instead. */
const FLOATING_LAUNCH_OFFER_TIMEOUT_MS = 2000;
/** Sent to a taker's page with `{ offerId, sessionId }`; the message itself stays in main. */
const FLOATING_LAUNCH_OFFER_CHANNEL = "omnigent:floating-composer:launch-offer";

/**
 * @param {{ timeoutMs?: number }} [options]
 */
function createFloatingLaunchOffers(options = {}) {
  const timeoutMs = options.timeoutMs ?? FLOATING_LAUNCH_OFFER_TIMEOUT_MS;
  /** @type {Set<object>} webContents whose page subscribed for offers */
  const takers = new Set();
  /** @type {Map<number, object>} open offers by id: { contents, message, taken, timer, settle } */
  const offers = new Map();
  let nextOfferId = 1;

  /** Ends an offer once; `confirmed` tells the panel the page sent the message. */
  function end(offerId, confirmed) {
    const offer = offers.get(offerId);
    if (offer === undefined) return;
    offers.delete(offerId);
    clearTimeout(offer.timer);
    offer.settle(confirmed);
  }

  return {
    /** The page subscribed. */
    addTaker(contents) {
      takers.add(contents);
    },
    /** The page unsubscribed, or its document went; its outstanding offers end now. */
    forgetTaker(contents) {
      takers.delete(contents);
      for (const [offerId, offer] of offers) if (offer.contents === contents) end(offerId, false);
    },
    /** @returns {boolean} */
    isTaker(contents) {
      return takers.has(contents);
    },
    /**
     * Offers `message` to a taker, which hears about it at once. Resolves true only once the
     * page confirms, and false when it doesn't in time or can't be reached.
     *
     * @param {object} contents
     * @param {{ sessionId: string, message: object }} launch
     * @returns {Promise<boolean>}
     */
    offer(contents, { sessionId, message }) {
      const offerId = nextOfferId;
      nextOfferId += 1;
      let settle = () => {};
      const confirmed = new Promise((resolve) => {
        settle = resolve;
      });
      const timer = setTimeout(() => end(offerId, false), timeoutMs);
      timer.unref?.();
      offers.set(offerId, { contents, message, taken: false, timer, settle });
      try {
        contents.send(FLOATING_LAUNCH_OFFER_CHANNEL, { offerId, sessionId });
      } catch {
        // The page is gone between the check and the send; the panel delivers.
        end(offerId, false);
      }
      return confirmed;
    },
    /**
     * The offered message, to the page it was offered to, once. Null for any other page or a
     * spent or ended offer.
     *
     * @returns {object | null}
     */
    take(contents, offerId) {
      const offer = offers.get(offerId);
      if (offer === undefined || offer.contents !== contents || offer.taken) return null;
      offer.taken = true;
      return offer.message;
    },
    /**
     * Whether the page's confirm lands while its offer is still open after a take. A late or
     * foreign confirm is refused, and the panel has already delivered or will.
     *
     * @returns {boolean}
     */
    confirm(contents, offerId) {
      const offer = offers.get(offerId);
      if (offer === undefined || offer.contents !== contents || !offer.taken) return false;
      end(offerId, true);
      return true;
    },
    /**
     * The page won't send the offered message, so the panel delivers it now rather than at the
     * timeout. Only the offered document may end its offer.
     *
     * @returns {boolean} whether the offer was ended
     */
    decline(contents, offerId) {
      const offer = offers.get(offerId);
      if (offer === undefined || offer.contents !== contents) return false;
      end(offerId, false);
      return true;
    },
  };
}

module.exports = {
  FLOATING_LAUNCH_OFFER_CHANNEL,
  FLOATING_LAUNCH_OFFER_TIMEOUT_MS,
  createFloatingLaunchOffers,
};
