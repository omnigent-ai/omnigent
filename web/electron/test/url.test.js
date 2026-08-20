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
  stripApiMountFromServerUrl,
  expandDatabricksWorkspaceUrl,
  WORKSPACE_UI_PATH,
  fetchServerManifest,
  PRE_MANIFEST_BASELINE,
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

  it("preserves the parser error when rejecting an invalid URL", () => {
    assert.throws(
      () => normalizeUrl("http://["),
      (error) => {
        assert.match(error.message, /invalid URL/);
        assert.ok(error.cause instanceof TypeError);
        return true;
      },
    );
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

describe("stripApiMountFromServerUrl", () => {
  it("strips a trailing /api/2.0/omnigent to the workspace root", () => {
    assert.equal(
      stripApiMountFromServerUrl("https://ws.cloud.databricks.com/api/2.0/omnigent"),
      "https://ws.cloud.databricks.com",
    );
  });

  it("handles the trailing-slash variant", () => {
    assert.equal(
      stripApiMountFromServerUrl("https://ws.cloud.databricks.com/api/2.0/omnigent/"),
      "https://ws.cloud.databricks.com",
    );
  });

  it("leaves a URL already at the workspace root unchanged", () => {
    assert.equal(
      stripApiMountFromServerUrl("https://ws.cloud.databricks.com"),
      "https://ws.cloud.databricks.com",
    );
    assert.equal(
      stripApiMountFromServerUrl("https://ws.cloud.databricks.com/"),
      "https://ws.cloud.databricks.com/",
    );
  });

  it("leaves an unrelated mount (e.g. the SPA path) unchanged", () => {
    assert.equal(
      stripApiMountFromServerUrl("https://ws.cloud.databricks.com/ml/omnigents"),
      "https://ws.cloud.databricks.com/ml/omnigents",
    );
  });

  it("returns empty/undefined/invalid input unchanged", () => {
    assert.equal(stripApiMountFromServerUrl(""), "");
    assert.equal(stripApiMountFromServerUrl(undefined), undefined);
    assert.equal(stripApiMountFromServerUrl("not a url"), "not a url");
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

  it("leaves a Databricks Apps host untouched, without probing", async () => {
    let probed = false;
    await withFetch(
      async () => {
        probed = true;
        return fakeResponse("databricks");
      },
      async () => {
        const url = "https://my-app-123.aws.databricksapps.com/";
        assert.equal(await expandDatabricksWorkspaceUrl(url), url);
        assert.equal(
          await expandDatabricksWorkspaceUrl("https://databricksapps.com/"),
          "https://databricksapps.com/",
        );
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

/** A JSON Response stand-in for the manifest fetch. */
function fakeJsonResponse(body, { ok = true, contentType = "application/json" } = {}) {
  return {
    ok,
    headers: { get: (name) => (name.toLowerCase() === "content-type" ? contentType : null) },
    json: async () => {
      if (typeof body === "string") throw new SyntaxError("Unexpected token");
      return body;
    },
  };
}

describe("fetchServerManifest", () => {
  it("reads a well-formed manifest", async () => {
    await withFetch(
      async (url) => {
        assert.equal(url, "http://localhost:6767/.well-known/omnigent.json");
        return fakeJsonResponse({
          manifest_version: 1,
          server_version: "0.6.0",
          min_desktop_version: null,
          ui: { server_picker: "sidebar" },
        });
      },
      async () => {
        const m = await fetchServerManifest("http://localhost:6767/");
        assert.equal(m.manifestVersion, 1);
        assert.equal(m.serverVersion, "0.6.0");
        assert.equal(m.minDesktopVersion, null);
        assert.equal(m.ui.server_picker, "sidebar");
      },
    );
  });

  it("treats a 404 as the pre-manifest baseline, not an error", async () => {
    // Every server older than the manifest route 404s here. This is THE
    // backwards-compat path: a new shell against an old server must connect
    // normally, so the absence resolves to a usable manifest.
    await withFetch(
      async () => fakeJsonResponse({}, { ok: false }),
      async () => {
        const m = await fetchServerManifest("http://localhost:6767/");
        assert.deepEqual(m, PRE_MANIFEST_BASELINE);
        // Fails a `>= 1` gate without the caller testing for null.
        assert.ok(!(m.manifestVersion >= 1));
      },
    );
  });

  it("ignores an HTML body from an SPA catch-all", async () => {
    // An older server without `.well-known` on its API allowlist serves
    // index.html with 200 for unknown paths. Parsing that as a manifest would
    // be worse than having none, so the content type is checked.
    await withFetch(
      async () => fakeJsonResponse("<!doctype html>", { contentType: "text/html; charset=utf-8" }),
      async () => {
        assert.deepEqual(
          await fetchServerManifest("http://localhost:6767/"),
          PRE_MANIFEST_BASELINE,
        );
      },
    );
  });

  it("falls back when the server is unreachable", async () => {
    await withFetch(
      async () => {
        throw new Error("ECONNREFUSED");
      },
      async () => {
        assert.deepEqual(
          await fetchServerManifest("http://localhost:6767/"),
          PRE_MANIFEST_BASELINE,
        );
      },
    );
  });

  it("falls back on malformed JSON", async () => {
    await withFetch(
      async () => fakeJsonResponse("{ not json"),
      async () => {
        assert.deepEqual(
          await fetchServerManifest("http://localhost:6767/"),
          PRE_MANIFEST_BASELINE,
        );
      },
    );
  });

  it("falls back when manifest_version is missing or not a number", async () => {
    // Each body swaps the process-global fetch, so the cases must run serially.
    /* oxlint-disable no-await-in-loop */
    for (const body of [{}, { manifest_version: "1" }, { manifest_version: NaN }, null, 42]) {
      await withFetch(
        async () => fakeJsonResponse(body),
        async () => {
          assert.deepEqual(
            await fetchServerManifest("http://localhost:6767/"),
            PRE_MANIFEST_BASELINE,
            `expected baseline for body ${JSON.stringify(body)}`,
          );
        },
      );
    }
    /* oxlint-enable no-await-in-loop */
  });

  it("keeps a NEWER envelope usable and preserves unknown fields", async () => {
    // The other direction: an OLD shell against a NEWER server. A bumped
    // envelope and unfamiliar ui shapes must not be rejected — the shell reads
    // what it knows and passes the rest through, which is what lets the server
    // add capabilities without waiting for shells to update.
    await withFetch(
      async () =>
        fakeJsonResponse({
          manifest_version: 99,
          server_version: "9.9.9",
          min_desktop_version: null,
          ui: { server_picker: "some-future-shape", future_thing: { nested: true } },
          brand_new_top_level_key: "ignored but harmless",
        }),
      async () => {
        const m = await fetchServerManifest("http://localhost:6767/");
        assert.equal(m.manifestVersion, 99);
        assert.ok(m.manifestVersion >= 1, "a newer envelope still passes a >=1 gate");
        assert.equal(m.ui.server_picker, "some-future-shape");
        assert.deepEqual(m.ui.future_thing, { nested: true });
      },
    );
  });

  it("coerces wrong-typed optional fields instead of failing", async () => {
    await withFetch(
      async () =>
        fakeJsonResponse({
          manifest_version: 1,
          server_version: 42,
          min_desktop_version: [],
          ui: "not-an-object",
        }),
      async () => {
        const m = await fetchServerManifest("http://localhost:6767/");
        // The envelope is valid, so the manifest is used — but each unusable
        // field degrades on its own rather than discarding the whole document.
        assert.equal(m.manifestVersion, 1);
        assert.equal(m.serverVersion, null);
        assert.equal(m.minDesktopVersion, null);
        assert.deepEqual(m.ui, {});
      },
    );
  });

  it("returns the baseline for an unparseable server URL", async () => {
    assert.deepEqual(await fetchServerManifest("not a url"), PRE_MANIFEST_BASELINE);
  });

  it("requests the manifest at the origin root, ignoring any path", async () => {
    // A workspace-mounted server (…/ml/omnigents) still serves the manifest at
    // the ORIGIN root — well-known URIs are origin-scoped by RFC 8615.
    await withFetch(
      async (url) => {
        assert.equal(url, "https://ws.example.com/.well-known/omnigent.json");
        return fakeJsonResponse({ manifest_version: 1 });
      },
      async () => {
        const m = await fetchServerManifest("https://ws.example.com/ml/omnigents");
        assert.equal(m.manifestVersion, 1);
      },
    );
  });
});
