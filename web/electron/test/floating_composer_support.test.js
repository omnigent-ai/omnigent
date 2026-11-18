"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  createFloatingComposerSupport,
  floatingComposerTarget,
  isServerSignInPage,
  mainWindowsByRecency,
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
  support.announce(1, "https://built-in.local");
  assert.equal(support.revoke(1), true);
  assert.equal(support.supports(1, "https://built-in.local"), false);
  assert.equal(support.revoke(1), false);
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

test("a sign-in page is the server's own login, register or OIDC route, under its mount", () => {
  const server = "https://host.example/ml/omnigents";
  assert.equal(isServerSignInPage(`${server}/login`, server), true);
  assert.equal(isServerSignInPage(`${server}/register?next=/`, server), true);
  assert.equal(isServerSignInPage(`${server}/auth/login`, server), true);
  assert.equal(isServerSignInPage(`${server}/login`, `${server}/`), true);
  assert.equal(isServerSignInPage("https://host.example/login", "https://host.example"), true);
  assert.equal(isServerSignInPage(`${server}/c/abc`, server), false);
  assert.equal(isServerSignInPage(`${server}/loginx`, server), false);
  // Another mount on the same origin, or a longer one sharing its prefix, isn't this server.
  assert.equal(isServerSignInPage("https://host.example/ml/other/login", server), false);
  assert.equal(isServerSignInPage("https://host.example/ml/omnigentsx/login", server), false);
  assert.equal(isServerSignInPage("https://other.example/ml/omnigents/login", server), false);
  assert.equal(isServerSignInPage(`${server}/login`, null), false);
  assert.equal(isServerSignInPage("not a url", server), false);
});
