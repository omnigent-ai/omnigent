"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  createFloatingComposerSupport,
  floatingComposerTarget,
  mainWindowsByRecency,
  serverPageOf,
} = require("../src/floating_composer_support");

test("support belongs to one document of one server", () => {
  const support = createFloatingComposerSupport();
  assert.equal(support.announce(1, "https://host.example/a"), true);
  assert.equal(support.announce(1, "https://host.example/a"), false);
  // A capable bundle under /a says nothing about an older one under /b on the same origin.
  assert.equal(support.supports(1, "https://host.example/a"), true);
  assert.equal(support.supports(1, "https://host.example/b"), false);
  assert.equal(support.supports(2, "https://host.example/a"), false);
});

test("a replaced document loses support until it announces again", () => {
  const support = createFloatingComposerSupport();
  support.announce(1, "https://local.example");
  assert.equal(support.revoke(1), true);
  assert.equal(support.supports(1, "https://local.example"), false);
  assert.equal(support.revoke(1), false);
  assert.equal(support.announce(1, "https://local.example"), true);
  assert.equal(support.supports(1, "https://local.example"), true);
});

test("knows whether any document can render the panel", () => {
  const support = createFloatingComposerSupport();
  assert.equal(support.any(), false);
  support.announce(1, "https://a.example");
  support.announce(2, "https://b.example");
  support.revoke(1);
  assert.equal(support.any(), true);
  support.revoke(2);
  assert.equal(support.any(), false);
});

function facts(overrides) {
  const servers = new Map([
    ["A", "https://a.example"],
    ["B", "https://b.example"],
  ]);
  return {
    focused: null,
    panelServerUrl: (win) => (win === "panelB" ? "https://b.example" : null),
    mainWindowsByRecency: ["A", "B"],
    serverUrlOf: (win) => servers.get(win) ?? null,
    supported: () => true,
    ...overrides,
  };
}

test("a focused panel keeps its own server, so the accelerator refocuses it", () => {
  assert.equal(floatingComposerTarget(facts({ focused: "panelB" })), "https://b.example");
});

test("otherwise the most recently focused main window decides", () => {
  assert.equal(
    floatingComposerTarget(facts({ mainWindowsByRecency: ["B", "A"] })),
    "https://b.example",
  );
  // It doesn't fall back to another window when that one's document can't render the panel.
  assert.equal(
    floatingComposerTarget(
      facts({ mainWindowsByRecency: ["B", "A"], supported: (win) => win === "A" }),
    ),
    null,
  );
  assert.equal(floatingComposerTarget(facts({ mainWindowsByRecency: [] })), null);
});

test("windows by recency: focused ones first, then the rest in creation order", () => {
  assert.deepEqual(mainWindowsByRecency(["B", "gone"], ["A", "B", "C"]), ["B", "A", "C"]);
});

test("names which page of its server a URL shows, under the server's mount only", () => {
  const server = "https://host.example/ml/omnigents";
  const cases = [
    [`${server}/compose/floating`, "panel"],
    [`${server}/compose/floating?draft=1#top`, "panel"],
    [`${server}/login`, "sign-in"],
    [`${server}/register?next=/`, "sign-in"],
    [`${server}/auth/callback`, "sign-in"],
    [`${server}`, "app"],
    [`${server}/`, "app"],
    [`${server}/c/abc`, "app"],
    [`${server}/loginx`, "app"],
    [`${server}/compose/floating/extra`, "app"],
    [`${server}/c/compose/floating`, "app"],
    // Another server on the same origin, even with a matching suffix, or a longer mount.
    ["https://host.example/ml/other/compose/floating", null],
    ["https://host.example/ml/omnigentsx/compose/floating", null],
    ["https://host.example/compose/floating", null],
    ["https://host.example/login", null],
    // Another origin, the setup page, or no URL at all.
    ["https://other.example/ml/omnigents/compose/floating", null],
    ["http://host.example/ml/omnigents/compose/floating", null],
    ["file:///app/setup/index.html", null],
    ["", null],
    ["not a url", null],
  ];
  for (const [url, page] of cases) assert.equal(serverPageOf(url, server), page, url);
  // A trailing slash on the server is the same mount; a server at the root owns every path.
  assert.equal(serverPageOf(`${server}/compose/floating`, `${server}/`), "panel");
  assert.equal(
    serverPageOf("https://host.example/compose/floating", "https://host.example"),
    "panel",
  );
  assert.equal(serverPageOf("https://host.example/x/login", "https://host.example/"), "app");
  assert.equal(serverPageOf("https://host.example/login", "https://host.example/"), "sign-in");
  assert.equal(serverPageOf(`${server}/login`, null), null);
});
