"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  FLOATING_LAUNCH_OFFER_CHANNEL,
  FLOATING_LAUNCH_TAKE_TIMEOUT_MS,
  createFloatingLaunchOffers,
} = require("../src/floating_launch_offers");

/**
 * Timers the test fires by hand, so an expiry needs no real wait. Every callback it was given is
 * kept, so a test can still call one after it was cleared.
 */
function fakeTimers() {
  const pending = new Map();
  const issued = [];
  let nextTimer = 1;
  return {
    setTimeout(callback) {
      const timer = nextTimer++;
      pending.set(timer, callback);
      issued.push({ timer, callback });
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
    /** The callback of the latest timer, cleared or not. */
    latestCallback: () => issued.at(-1).callback,
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

function setUp() {
  const timers = fakeTimers();
  const offers = createFloatingLaunchOffers({
    timeoutMs: FLOATING_LAUNCH_TAKE_TIMEOUT_MS,
    startTimer: timers.setTimeout,
    stopTimer: timers.clearTimeout,
  });
  return { offers, timers };
}

const target = { sessionId: "conv_1", path: "/c/conv_1" };
const message = { text: "Fix the build", skill: null, files: [] };

test("a page takes the message, confirms it, and the offer is confirmed once", async () => {
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
  assert.equal(offers.confirm(contents, payload.id), true);
  assert.equal(await settled, "confirmed");
  assert.equal(timers.count(), 0, "the expiry is cleared once confirmed");
  assert.equal(offers.confirm(contents, payload.id), false, "a second confirm gets nothing");
});

test("taking doesn't settle the offer: the page must still confirm before the panel stands down", async () => {
  const { offers, timers } = setUp();
  const contents = page();
  const settled = offers.offer(contents, target, message);
  const { id } = contents.sent[0].payload;
  offers.take(contents, id);
  timers.fire();
  assert.equal(await settled, "handed-out", "expiry ends a taken, unconfirmed offer as handed out");
  assert.equal(offers.confirm(contents, id), false, "a late confirm gets nothing");
});

test("an offer no page takes in time settles not-taken, and a take after that gets nothing", async () => {
  const { offers, timers } = setUp();
  const contents = page();
  const settled = offers.offer(contents, target, message);
  const { id } = contents.sent[0].payload;
  timers.fire();
  assert.equal(await settled, "not-taken");
  assert.equal(offers.take(contents, id), null, "a late take after expiry is refused");
  assert.equal(offers.confirm(contents, id), false);
});

test("only the document an offer went to can take or confirm it", () => {
  const { offers } = setUp();
  const offered = page();
  const other = page();
  offers.registerTaker(offered);
  offers.offer(offered, target, message);
  const { id } = offered.sent[0].payload;
  assert.equal(offers.take(other, id), null);
  assert.equal(offers.take(offered, id), message, "the refused take didn't use up the offer");
  assert.equal(offers.confirm(other, id), false, "another document can't confirm it");
  assert.equal(offers.confirm(offered, id), true);
});

test("a confirm before any take is refused", async () => {
  const { offers } = setUp();
  const contents = page();
  const settled = offers.offer(contents, target, message);
  const { id } = contents.sent[0].payload;
  assert.equal(offers.confirm(contents, id), false);
  offers.forget(contents);
  assert.equal(await settled, "not-taken");
});

test("a document that goes before the take ends its offers at once, and takes no more", async () => {
  const { offers, timers } = setUp();
  const contents = page();
  offers.registerTaker(contents);
  const settled = offers.offer(contents, target, message);
  offers.forget(contents);
  assert.equal(await settled, "not-taken", "the panel doesn't wait out the timeout");
  assert.equal(timers.count(), 0);
  assert.equal(offers.canOffer(contents), false);
});

test("a document that goes between take and confirm ends the offer as handed out", async () => {
  const { offers } = setUp();
  const contents = page();
  const settled = offers.offer(contents, target, message);
  const { id } = contents.sent[0].payload;
  offers.take(contents, id);
  offers.forget(contents);
  assert.equal(await settled, "handed-out", "the panel delivers; nothing was queued elsewhere");
  assert.equal(offers.confirm(contents, id), false, "a confirm after the interruption is refused");
});

test("an offer the page can't be sent to settles not-taken at once", async () => {
  const { offers, timers } = setUp();
  const contents = page({
    send() {
      throw new Error("Object has been destroyed");
    },
  });
  assert.equal(await offers.offer(contents, target, message), "not-taken");
  assert.equal(timers.count(), 0);
});

test("a late confirm and a stale expiry settle an offer once between them", async () => {
  const { offers, timers } = setUp();
  const contents = page();
  const settled = offers.offer(contents, target, message);
  const { id } = contents.sent[0].payload;
  const staleExpiry = timers.latestCallback();
  offers.take(contents, id);
  assert.equal(offers.confirm(contents, id), true);
  // The expiry timer was cleared by the confirm; calling its callback anyway must change nothing.
  staleExpiry();
  assert.equal(await settled, "confirmed", "the stale expiry doesn't flip the answer");
  assert.equal(offers.take(contents, id), null);
  assert.equal(offers.confirm(contents, id), false);
  assert.equal(timers.count(), 0);
});

test("a stale expiry after a take and before confirm ends the offer once, handed out", async () => {
  const { offers, timers } = setUp();
  const contents = page();
  const settled = offers.offer(contents, target, message);
  const { id } = contents.sent[0].payload;
  const staleExpiry = timers.latestCallback();
  offers.take(contents, id);
  timers.fire();
  assert.equal(await settled, "handed-out");
  // The same expiry callback again: the offer is already settled, so nothing changes.
  staleExpiry();
  assert.equal(offers.confirm(contents, id), false);
  assert.equal(offers.take(contents, id), null);
});

test("a taken offer ends as document-gone when its document goes, and as handed-out when the page unsubscribes", async () => {
  const { offers } = setUp();
  const gone = page();
  const goneSettled = offers.offer(gone, target, message);
  offers.take(gone, gone.sent[0].payload.id);
  offers.forget(gone, { documentGone: true });
  assert.equal(await goneSettled, "document-gone");

  const unsubscribed = page();
  const unsubscribedSettled = offers.offer(unsubscribed, target, message);
  offers.take(unsubscribed, unsubscribed.sent[0].payload.id);
  offers.forget(unsubscribed);
  assert.equal(
    await unsubscribedSettled,
    "handed-out",
    "the page is still there, holding the message",
  );
});
