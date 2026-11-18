// Tests for View → Open in Browser's URL choice (src/openInBrowser.js).

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { browserUrlFor } = require("../src/openInBrowser");

const PINNED = "http://127.0.0.1:6767";

describe("browserUrlFor", () => {
  it("opens the current page on the pinned server, query included", () => {
    assert.equal(
      browserUrlFor(`${PINNED}/tiles?view=focus&active=conv_a`, PINNED),
      `${PINNED}/tiles?view=focus&active=conv_a`,
    );
  });

  it("refuses pages off the pinned server", () => {
    assert.equal(browserUrlFor("https://example.com/c/conv_a", PINNED), null);
    assert.equal(browserUrlFor("file:///app/connection_loading.html", PINNED), null);
  });

  it("refuses an unpinned window or an unreadable URL", () => {
    assert.equal(browserUrlFor(`${PINNED}/`, null), null);
    assert.equal(browserUrlFor("not a url", PINNED), null);
    assert.equal(browserUrlFor(undefined, PINNED), null);
  });
});
