"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  FLOATING_LAUNCH_OFFER_CHANNEL,
  FLOATING_LAUNCH_OFFER_TIMEOUT_MS,
  createFloatingLaunchOffers,
} = require("../src/floating_launch_offers");

/** A main window's webContents that records what it's sent. */
function page(extra = {}) {
  return {
    sent: [],
    send(channel, data) {
      this.sent.push([channel, data]);
    },
    ...extra,
  };
}

const message = { text: "Fix the build", skill: null, files: [] };

test("an offer reaches only a page that subscribed, and forgetting one stops it", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const offers = createFloatingLaunchOffers();
  const subscribed = page();
  assert.equal(offers.isTaker(subscribed), false);
  offers.addTaker(subscribed);
  assert.equal(offers.isTaker(subscribed), true);
  offers.forgetTaker(subscribed);
  assert.equal(offers.isTaker(subscribed), false);
});

test("a taker takes the message once, then its confirm settles the offer as sent", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const offers = createFloatingLaunchOffers();
  const taker = page();
  offers.addTaker(taker);
  const confirmed = offers.offer(taker, { sessionId: "conv_1", message });
  const [channel, { offerId, sessionId }] = taker.sent[0];
  assert.equal(channel, FLOATING_LAUNCH_OFFER_CHANNEL);
  assert.equal(sessionId, "conv_1");
  assert.equal(offers.take(taker, offerId), message);
  assert.equal(offers.take(taker, offerId), null, "a message is taken once");
  assert.equal(offers.confirm(taker, offerId), true);
  assert.equal(await confirmed, true);
  assert.equal(offers.confirm(taker, offerId), false, "a settled offer confirms nothing");
});

test("only the page an offer went to can take or confirm it", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const offers = createFloatingLaunchOffers();
  const taker = page();
  const stranger = page();
  offers.addTaker(taker);
  offers.offer(taker, { sessionId: "conv_1", message });
  const { offerId } = taker.sent[0][1];
  assert.equal(offers.take(stranger, offerId), null);
  assert.equal(offers.take(taker, offerId), message);
  assert.equal(offers.confirm(stranger, offerId), false);
  assert.equal(offers.confirm(taker, offerId), true);
});

test("a confirm before any take is refused, and the offer then times out", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const offers = createFloatingLaunchOffers();
  const taker = page();
  offers.addTaker(taker);
  const confirmed = offers.offer(taker, { sessionId: "conv_1", message });
  const { offerId } = taker.sent[0][1];
  assert.equal(offers.confirm(taker, offerId), false);
  t.mock.timers.tick(FLOATING_LAUNCH_OFFER_TIMEOUT_MS - 1);
  assert.equal(offers.take(taker, offerId), message, "still open a tick before the timeout");
  t.mock.timers.tick(1);
  assert.equal(await confirmed, false);
});

test("a taken offer the page never confirms times out, and a late confirm is refused", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const offers = createFloatingLaunchOffers();
  const taker = page();
  offers.addTaker(taker);
  const confirmed = offers.offer(taker, { sessionId: "conv_1", message });
  const { offerId } = taker.sent[0][1];
  offers.take(taker, offerId);
  t.mock.timers.tick(FLOATING_LAUNCH_OFFER_TIMEOUT_MS);
  assert.equal(await confirmed, false);
  assert.equal(offers.confirm(taker, offerId), false, "the panel has delivered it by now");
});

test("a page that goes or unsubscribes ends its outstanding offers at once", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const offers = createFloatingLaunchOffers();
  const taker = page();
  offers.addTaker(taker);
  const confirmed = offers.offer(taker, { sessionId: "conv_1", message });
  const { offerId } = taker.sent[0][1];
  offers.take(taker, offerId);
  offers.forgetTaker(taker);
  assert.equal(await confirmed, false);
  assert.equal(offers.confirm(taker, offerId), false);
});

test("an offer whose page can't be reached settles false at once", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const offers = createFloatingLaunchOffers();
  const destroyed = page({
    send() {
      throw new Error("Object has been destroyed");
    },
  });
  offers.addTaker(destroyed);
  assert.equal(await offers.offer(destroyed, { sessionId: "conv_1", message }), false);
});
