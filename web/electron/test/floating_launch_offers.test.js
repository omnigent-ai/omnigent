"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  FLOATING_LAUNCH_OFFER_CHANNEL,
  FLOATING_LAUNCH_TAKE_TIMEOUT_MS,
  createFloatingLaunchOffers,
} = require("../src/floating_launch_offers");

/** Timers the test fires by hand, so an expiry needs no real wait. */
function fakeTimers() {
  const pending = new Map();
  let nextTimer = 1;
  return {
    setTimeout(callback) {
      const timer = nextTimer++;
      pending.set(timer, callback);
      return timer;
    },
    clearTimeout(timer) {
      pending.delete(timer);
    },
    fire() {
      for (const [timer, callback] of [...pending]) {
        pending.delete(timer);
        callback();
      }
    },
    count: () => pending.size,
  };
}

/** A page's webContents: records what it was sent, and can be made unreachable. */
function page(extra = {}) {
  return {
    sent: [],
    send(channel, payload) {
      this.sent.push({ channel, payload });
    },
    ...extra,
  };
}

function setUp(options = {}) {
  const timers = fakeTimers();
  const offers = createFloatingLaunchOffers({
    timeoutMs: FLOATING_LAUNCH_TAKE_TIMEOUT_MS,
    startTimer: timers.setTimeout,
    stopTimer: timers.clearTimeout,
    ...options,
  });
  return { offers, timers };
}

const target = { sessionId: "conv_1", path: "/c/conv_1" };
const message = { text: "Fix the build", skill: null, files: [] };

test("a page that takes the offer in time gets the message once, and the offer ends", async () => {
  const { offers, timers } = setUp();
  const contents = page();
  offers.registerTaker(contents);
  assert.equal(offers.canOffer(contents), true);
  const settled = offers.offer(contents, target, message);
  const [{ channel, payload }] = contents.sent;
  assert.equal(channel, FLOATING_LAUNCH_OFFER_CHANNEL);
  assert.deepEqual(payload, { id: payload.id, sessionId: "conv_1", path: "/c/conv_1" });
  assert.equal(offers.take(contents, payload.id), message);
  assert.equal(offers.take(contents, payload.id), null, "a second take gets nothing");
  assert.equal(await settled, true);
  assert.equal(timers.count(), 0, "the expiry is cleared once the offer is taken");
});

test("an offer no page takes in time settles false, and a take after that gets nothing", async () => {
  const { offers, timers } = setUp();
  const contents = page();
  const settled = offers.offer(contents, target, message);
  const { id } = contents.sent[0].payload;
  timers.fire();
  assert.equal(await settled, false);
  assert.equal(offers.take(contents, id), null, "a late take after expiry is refused");
});

test("only the document an offer went to can take it", () => {
  const { offers } = setUp();
  const offered = page();
  const other = page();
  offers.registerTaker(offered);
  offers.offer(offered, target, message);
  const { id } = offered.sent[0].payload;
  assert.equal(offers.take(other, id), null);
  assert.equal(offers.take(offered, id), message, "the refused take didn't use up the offer");
});

test("a document that goes ends its pending offers at once, and takes no more", async () => {
  const { offers, timers } = setUp();
  const contents = page();
  offers.registerTaker(contents);
  const settled = offers.offer(contents, target, message);
  offers.forget(contents);
  assert.equal(await settled, false, "the panel doesn't wait out the timeout");
  assert.equal(timers.count(), 0);
  assert.equal(offers.canOffer(contents), false);
});

test("an offer the page can't be sent to settles false at once", async () => {
  const { offers, timers } = setUp();
  const contents = page({
    send() {
      throw new Error("Object has been destroyed");
    },
  });
  assert.equal(await offers.offer(contents, target, message), false);
  assert.equal(timers.count(), 0);
});

test("the take and the expiry settle an offer once between them", async () => {
  const { offers, timers } = setUp();
  const contents = page();
  const settled = offers.offer(contents, target, message);
  const { id } = contents.sent[0].payload;
  assert.equal(offers.take(contents, id), message);
  timers.fire();
  assert.equal(await settled, true, "the later expiry doesn't flip the answer");
  assert.equal(offers.take(contents, id), null);
});
