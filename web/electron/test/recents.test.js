// Tests for the recent-servers list helpers (src/recents.js), run with
// `node --test` (no extra deps). Covers the legacy-string tolerance, the
// nickname-preserving bump on re-connect, label set/clear, and the
// string-when-unlabelled serialization that keeps the on-disk format
// backward-compatible.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  MAX_RECENT_SERVERS,
  hostOf,
  normalizeRecents,
  rememberRecent,
  setLabel,
  serializeRecents,
} = require("../src/recents");

describe("normalizeRecents", () => {
  it("returns [] for a non-array (missing/corrupt settings)", () => {
    assert.deepEqual(normalizeRecents(undefined), []);
    assert.deepEqual(normalizeRecents(null), []);
    assert.deepEqual(normalizeRecents("nope"), []);
  });

  it("reads a legacy string entry as an unlabelled server", () => {
    assert.deepEqual(normalizeRecents(["http://localhost:6767"]), [
      { url: "http://localhost:6767", label: "" },
    ]);
  });

  it("reads a { url, label } object entry as-is", () => {
    assert.deepEqual(normalizeRecents([{ url: "https://a.example.com", label: "Prod" }]), [
      { url: "https://a.example.com", label: "Prod" },
    ]);
  });

  it("tolerates a mix of strings and objects", () => {
    assert.deepEqual(
      normalizeRecents([{ url: "https://a.example.com", label: "Prod" }, "http://localhost:6767"]),
      [
        { url: "https://a.example.com", label: "Prod" },
        { url: "http://localhost:6767", label: "" },
      ],
    );
  });

  it("drops junk entries (non-string url, empty url, non-string label)", () => {
    assert.deepEqual(
      normalizeRecents([
        42,
        null,
        {},
        { url: 5 },
        { url: "" },
        { url: "https://ok.example.com", label: 7 },
      ]),
      [{ url: "https://ok.example.com", label: "" }],
    );
  });

  it("deduplicates by url, keeping the first (most recent) occurrence", () => {
    assert.deepEqual(
      normalizeRecents([
        { url: "https://a.example.com", label: "First" },
        { url: "https://a.example.com", label: "Later dup" },
      ]),
      [{ url: "https://a.example.com", label: "First" }],
    );
  });
});

describe("rememberRecent", () => {
  it("puts a new url at the head", () => {
    const list = [{ url: "https://a.example.com", label: "" }];
    assert.deepEqual(rememberRecent(list, "http://localhost:6767"), [
      { url: "http://localhost:6767", label: "" },
      { url: "https://a.example.com", label: "" },
    ]);
  });

  it("bumps an existing url to the head without duplicating it", () => {
    const list = [
      { url: "https://a.example.com", label: "" },
      { url: "http://localhost:6767", label: "" },
    ];
    assert.deepEqual(rememberRecent(list, "http://localhost:6767"), [
      { url: "http://localhost:6767", label: "" },
      { url: "https://a.example.com", label: "" },
    ]);
  });

  it("preserves an existing nickname when a url bumps to the head", () => {
    const list = [
      { url: "https://a.example.com", label: "" },
      { url: "http://localhost:6767", label: "Local dev" },
    ];
    assert.deepEqual(rememberRecent(list, "http://localhost:6767"), [
      { url: "http://localhost:6767", label: "Local dev" },
      { url: "https://a.example.com", label: "" },
    ]);
  });

  it("caps the list at MAX_RECENT_SERVERS", () => {
    let list = [];
    for (let i = 0; i < MAX_RECENT_SERVERS + 3; i += 1) {
      list = rememberRecent(list, `https://s${i}.example.com`);
    }
    assert.equal(list.length, MAX_RECENT_SERVERS);
    // The most recently remembered url leads the list.
    assert.equal(list[0].url, `https://s${MAX_RECENT_SERVERS + 2}.example.com`);
  });
});

describe("setLabel", () => {
  const base = [
    { url: "https://a.example.com", label: "" },
    { url: "http://localhost:6767", label: "old" },
  ];

  it("sets a nickname on the matching url only", () => {
    assert.deepEqual(setLabel(base, "https://a.example.com", "Prod"), [
      { url: "https://a.example.com", label: "Prod" },
      { url: "http://localhost:6767", label: "old" },
    ]);
  });

  it("trims surrounding whitespace", () => {
    assert.equal(setLabel(base, "https://a.example.com", "  Prod  ")[0].label, "Prod");
  });

  it("clears the nickname when given a blank/whitespace label", () => {
    assert.equal(setLabel(base, "http://localhost:6767", "   ")[1].label, "");
    assert.equal(setLabel(base, "http://localhost:6767", "")[1].label, "");
  });

  it("is a no-op for a url not in the list", () => {
    assert.deepEqual(setLabel(base, "https://unknown.example.com", "X"), base);
  });
});

describe("serializeRecents", () => {
  it("writes an unlabelled entry as a bare string", () => {
    assert.deepEqual(serializeRecents([{ url: "http://localhost:6767", label: "" }]), [
      "http://localhost:6767",
    ]);
  });

  it("writes a labelled entry as a { url, label } object", () => {
    assert.deepEqual(serializeRecents([{ url: "https://a.example.com", label: "Prod" }]), [
      { url: "https://a.example.com", label: "Prod" },
    ]);
  });

  it("round-trips through normalizeRecents", () => {
    const list = [
      { url: "https://a.example.com", label: "Prod" },
      { url: "http://localhost:6767", label: "" },
    ];
    assert.deepEqual(normalizeRecents(serializeRecents(list)), list);
  });
});

describe("hostOf", () => {
  it("returns the host for a valid URL", () => {
    assert.equal(hostOf("https://a.example.com/ml/omnigent"), "a.example.com");
    assert.equal(hostOf("http://localhost:6767"), "localhost:6767");
  });

  it("falls back to the raw input when unparseable", () => {
    assert.equal(hostOf("not a url"), "not a url");
  });
});
