// Tests for the shared desktop URL helpers (src/url.js), run with
// `node --test` (no extra deps). Covers the scheme-defaulting that lets a
// pasted workspace URL (schemeless, /omnigent suffix from the internal user
// guide) connect, the plain-http warning, and the workspace probe/expansion.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  defaultSchemeFor,
  normalizeUrl,
  isPlainHttpRemote,
  expandDatabricksWorkspaceUrl,
  WORKSPACE_UI_PATH,
} = require("../src/url");

describe("defaultSchemeFor", () => {
  it("defaults remote hosts to https", () => {
    assert.equal(defaultSchemeFor("dbc-x.cloud.databricks.com/omnigent"), "https");
    assert.equal(defaultSchemeFor("example.com"), "https");
  });

  it("defaults loopback hosts to http", () => {
    assert.equal(defaultSchemeFor("localhost:6767"), "http");
    assert.equal(defaultSchemeFor("127.0.0.1:6767"), "http");
    assert.equal(defaultSchemeFor("[::1]:6767"), "http");
  });

  it("defaults unparseable input to https", () => {
    assert.equal(defaultSchemeFor("exa mple"), "https");
  });
});

describe("normalizeUrl", () => {
  it("defaults a schemeless workspace /omnigent URL to https", () => {
    assert.equal(
      normalizeUrl("dbc-a5d4177a-49dc.cloud.databricks.com/omnigent"),
      "https://dbc-a5d4177a-49dc.cloud.databricks.com/omnigent",
    );
  });

  it("defaults a bare remote host to https", () => {
    assert.equal(
      normalizeUrl("example.cloud.databricks.com"),
      "https://example.cloud.databricks.com/",
    );
  });

  it("defaults loopback hosts to http", () => {
    assert.equal(normalizeUrl("localhost:6767"), "http://localhost:6767/");
    assert.equal(normalizeUrl("127.0.0.1:6767"), "http://127.0.0.1:6767/");
    assert.equal(normalizeUrl("[::1]:6767"), "http://[::1]:6767/");
  });

  it("preserves an explicit scheme (even http to a remote host)", () => {
    assert.equal(normalizeUrl("http://localhost:6767"), "http://localhost:6767/");
    assert.equal(normalizeUrl("https://example.com"), "https://example.com/");
    assert.equal(normalizeUrl("http://example.databricks.com"), "http://example.databricks.com/");
  });

  it("trims surrounding whitespace", () => {
    assert.equal(normalizeUrl("  example.com/omnigent  "), "https://example.com/omnigent");
  });

  it("rejects empty input", () => {
    assert.throws(() => normalizeUrl(""), /server URL is empty/);
    assert.throws(() => normalizeUrl("   "), /server URL is empty/);
  });

  it("rejects a non-http(s) scheme", () => {
    assert.throws(() => normalizeUrl("ftp://example.com"), /unsupported scheme/);
  });
});

describe("isPlainHttpRemote", () => {
  it("does not warn for a bare remote host (now https)", () => {
    assert.equal(isPlainHttpRemote("example.databricks.com"), false);
    assert.equal(isPlainHttpRemote("dbc-x.cloud.databricks.com/omnigent"), false);
  });

  it("warns for an explicit http:// to a remote host", () => {
    assert.equal(isPlainHttpRemote("http://example.databricks.com"), true);
  });

  it("does not warn for loopback hosts", () => {
    assert.equal(isPlainHttpRemote("localhost:6767"), false);
    assert.equal(isPlainHttpRemote("http://localhost:6767"), false);
    assert.equal(isPlainHttpRemote("http://127.0.0.1:6767"), false);
  });

  it("does not warn for https or empty/invalid input", () => {
    assert.equal(isPlainHttpRemote("https://example.databricks.com"), false);
    assert.equal(isPlainHttpRemote(""), false);
    assert.equal(isPlainHttpRemote("ht tp://nope"), false);
  });
});

/**
 * Run `fn` with `globalThis.fetch` swapped for `stub` and `AbortSignal.timeout`
 * neutralized (no real timer), restoring both afterward.
 */
async function withFetch(stub, fn) {
  const realFetch = globalThis.fetch;
  const realTimeout = AbortSignal.timeout;
  globalThis.fetch = stub;
  AbortSignal.timeout = () => new AbortController().signal;
  try {
    return await fn();
  } finally {
    globalThis.fetch = realFetch;
    AbortSignal.timeout = realTimeout;
  }
}

/** A minimal Response stand-in exposing only `.headers.get`. */
function fakeResponse(serverHeader) {
  return { headers: { get: (name) => (name === "server" ? serverHeader : null) } };
}

describe("expandDatabricksWorkspaceUrl", () => {
  it("expands a bare https Databricks workspace root to the UI mount", async () => {
    const calls = [];
    await withFetch(
      async (url, opts) => {
        calls.push({ url, method: opts.method });
        return fakeResponse("databricks");
      },
      async () => {
        const out = await expandDatabricksWorkspaceUrl("https://ws.cloud.databricks.com/");
        assert.equal(out, `https://ws.cloud.databricks.com${WORKSPACE_UI_PATH}`);
      },
    );
    // Probed the root with a HEAD request.
    assert.deepEqual(calls, [{ url: "https://ws.cloud.databricks.com/", method: "HEAD" }]);
  });

  it("leaves a non-Databricks root unchanged", async () => {
    await withFetch(
      async () => fakeResponse("nginx"),
      async () => {
        assert.equal(
          await expandDatabricksWorkspaceUrl("https://example.com"),
          "https://example.com",
        );
      },
    );
  });

  it("leaves a URL that already carries a path untouched, without probing", async () => {
    let probed = false;
    await withFetch(
      async () => {
        probed = true;
        return fakeResponse("databricks");
      },
      async () => {
        const url = "https://ws.cloud.databricks.com/omnigent";
        assert.equal(await expandDatabricksWorkspaceUrl(url), url);
      },
    );
    assert.equal(probed, false);
  });

  it("leaves a non-https URL untouched, without probing", async () => {
    let probed = false;
    await withFetch(
      async () => {
        probed = true;
        return fakeResponse("databricks");
      },
      async () => {
        assert.equal(
          await expandDatabricksWorkspaceUrl("http://localhost:6767/"),
          "http://localhost:6767/",
        );
      },
    );
    assert.equal(probed, false);
  });

  it("falls back to the input when the probe fails", async () => {
    await withFetch(
      async () => {
        throw new Error("ECONNREFUSED");
      },
      async () => {
        const url = "https://unreachable.example.com";
        assert.equal(await expandDatabricksWorkspaceUrl(url), url);
      },
    );
  });

  it("returns unparseable input unchanged", async () => {
    assert.equal(await expandDatabricksWorkspaceUrl("not a url"), "not a url");
  });
});
