const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  oidcServerUrlError,
  serverRoute,
  classifyAuthProbe,
  probeServerAuth,
  runOidcBrowserLogin,
  sessionCookieDetails,
  installAndVerifySessionCookie,
} = require("../src/oidc_auth");

function response(status, body = null) {
  return {
    status,
    json: async () => {
      if (body === null) throw new Error("not json");
      return body;
    },
  };
}

describe("OIDC provider detection", () => {
  it("joins /v1/me under a mounted server URL", () => {
    assert.equal(
      serverRoute("https://workspace.example/ml/omnigents/", "/v1/me"),
      "https://workspace.example/ml/omnigents/v1/me",
    );
  });

  it("keeps the organization selector on /v1/me", () => {
    assert.equal(
      serverRoute("https://dbc-a.cloud.databricks.com/omnigent?o=123456789", "/v1/me"),
      "https://dbc-a.cloud.databricks.com/omnigent/v1/me?o=123456789",
    );
  });

  it("gates only the OIDC login_url", () => {
    assert.equal(classifyAuthProbe(200, null), "authenticated");
    assert.equal(classifyAuthProbe(401, "/auth/login"), "oidc");
    assert.equal(classifyAuthProbe(401, "/login"), "accounts");
    assert.equal(classifyAuthProbe(401, null), "other");
    assert.equal(classifyAuthProbe(302, null), "other");
  });

  it("uses the Electron session and leaves redirects manual", async () => {
    const calls = [];
    const electronSession = {
      fetch: async (url, init) => {
        calls.push({ url, init });
        return response(401, { login_url: "/auth/login" });
      },
    };

    const result = await probeServerAuth(electronSession, "https://server.example/base");

    assert.deepEqual(result, { kind: "oidc", status: 401 });
    assert.equal(calls[0].url, "https://server.example/base/v1/me");
    assert.equal(calls[0].init.redirect, "manual");
    assert.equal(calls[0].init.credentials, "include");
  });
});

describe("OIDC browser ticket flow", () => {
  it("accepts canonical mounted, IPv6, IDNA, and UTF-8 server paths", () => {
    const serverUrls = [
      "https://server.example/base/path",
      "https://[2001:db8::1]:8443/base",
      "http://[::1]:6767/base",
      "https://xn--bcher-kva.example/base/%E2%9C%93",
      `https://server.example/${"a".repeat(2048)}`,
    ];
    for (const serverUrl of serverUrls) {
      assert.equal(oidcServerUrlError(serverUrl), null);
      assert.equal(serverRoute(serverUrl, "/v1/me"), `${serverUrl}/v1/me`);
    }
  });

  it("allows only organization selectors on Databricks workspace hosts", () => {
    assert.equal(
      oidcServerUrlError("https://dbc-a.cloud.databricks.com/omnigent?o=123456789"),
      null,
    );
    assert.equal(
      oidcServerUrlError("https://dbc-a.cloud.databricks.com/omnigent?o=123&o=456"),
      null,
    );
    assert.equal(
      oidcServerUrlError("https://server.example/base?o=123456789"),
      "invalid_server_url",
    );
    assert.equal(
      oidcServerUrlError("https://dbc-a.cloud.databricks.com/omnigent?o=123&view=chat"),
      "invalid_server_url",
    );
  });

  it("rejects non-loopback HTTP before any authentication side effect", async () => {
    let fetches = 0;
    let opens = 0;
    const result = await runOidcBrowserLogin(
      {
        fetch: async () => {
          fetches += 1;
          return response(200, { ticket: "secret", login_url: "/auth/login?ticket=secret" });
        },
      },
      "http://server.example",
      async () => {
        opens += 1;
      },
    );

    assert.deepEqual(result, { ok: false, reason: "insecure_transport" });
    assert.equal(fetches, 0);
    assert.equal(opens, 0);
  });

  it("fails closed on malformed and credentialed server URLs", async () => {
    await Promise.all(
      ["not a url", "https://user:secret@server.example"].map(async (serverUrl) => {
        let fetches = 0;
        let opens = 0;
        const result = await runOidcBrowserLogin(
          {
            fetch: async () => {
              fetches += 1;
            },
          },
          serverUrl,
          async () => {
            opens += 1;
          },
        );

        assert.deepEqual(result, { ok: false, reason: "invalid_server_url" });
        assert.equal(fetches, 0);
        assert.equal(opens, 0);
      }),
    );
  });

  it("rejects ambiguous configured server URLs before any side effect", async () => {
    const invalidServerUrls = [
      "https://server.example/base?workspace=one",
      "https://server.example/base?",
      "https://server.example/base#workspace",
      "https://server.example/base#",
      "https://server.example/base/../other",
      "https://server.example/base/%2e%2e/other",
      "https://server.example/base/%252e%252e/other",
      "https://server.example/base/%2525252e%2525252e/other",
      "https://server.example/base\\other",
      "https://server.example/base//other",
      "https://server.example/base/%2fother",
      "https://server.example/base/%252fother",
      "https://server.example/base/%2525252fother",
      `https://server.example/base/%${"25".repeat(40)}2fother`,
      "https://server.example/base/%",
      "https://server.example/base/%C0%AF",
      "https://server.example:/base",
      "https://server.example:443/base",
    ];

    await Promise.all(
      invalidServerUrls.map(async (serverUrl) => {
        let fetches = 0;
        let opens = 0;
        const result = await runOidcBrowserLogin(
          {
            fetch: async () => {
              fetches += 1;
              return response(200, { ticket: "secret", login_url: "/auth/login?ticket=secret" });
            },
          },
          serverUrl,
          async () => {
            opens += 1;
          },
        );

        assert.deepEqual(result, { ok: false, reason: "invalid_server_url" });
        assert.equal(fetches, 0);
        assert.equal(opens, 0);

        await assert.rejects(
          installAndVerifySessionCookie(
            {
              cookies: {
                get: async () => assert.fail("read cookies for an invalid server URL"),
                set: async () => assert.fail("set cookies for an invalid server URL"),
                remove: async () => assert.fail("removed cookies for an invalid server URL"),
              },
              fetch: async () => assert.fail("probed an invalid server URL"),
            },
            serverUrl,
            "session-jwt",
          ),
          /server URL is invalid/,
        );
      }),
    );
  });

  it("preserves OIDC ticket login over loopback HTTP, including IPv6", async () => {
    await Promise.all(
      ["http://localhost:6767", "http://127.0.0.1:6767", "http://[::1]:6767"].map(
        async (serverUrl) => {
          const responses = [
            response(200, { ticket: "secret", login_url: "/auth/login?ticket=secret" }),
            response(200, { token: "session-jwt" }),
          ];
          const opened = [];

          const result = await runOidcBrowserLogin(
            { fetch: async () => responses.shift() },
            serverUrl,
            async (url) => opened.push(url),
            { pollIntervalMs: 1, timeoutMs: 100 },
          );

          assert.deepEqual(result, { ok: true, token: "session-jwt" });
          assert.deepEqual(opened, [`${serverUrl}/auth/login?ticket=secret`]);
        },
      ),
    );
  });

  it("retries transient ticket creation statuses before opening the browser", async () => {
    const responses = [
      ...[429, 502, 503, 504].map((status) => response(status)),
      response(200, { ticket: "secret", login_url: "/auth/login?ticket=secret" }),
      response(200, { token: "session-jwt" }),
    ];
    const statuses = [];

    const result = await runOidcBrowserLogin(
      { fetch: async () => responses.shift() },
      "https://server.example",
      async () => {},
      {
        pollIntervalMs: 1,
        timeoutMs: 100,
        onPollError: (status) => statuses.push(status),
      },
    );

    assert.deepEqual(result, { ok: true, token: "session-jwt" });
    assert.deepEqual(statuses, [429, 502, 503, 504]);
  });

  it("does not retry permanent ticket creation failures", async () => {
    const results = await Promise.all(
      [401, 403, 400].map(async (status) => {
        let requests = 0;
        const result = await runOidcBrowserLogin(
          {
            fetch: async () => {
              requests += 1;
              return response(status);
            },
          },
          "https://server.example",
          async () => {},
          { pollIntervalMs: 1, timeoutMs: 100 },
        );
        return { requests, result };
      }),
    );
    assert.deepEqual(
      results,
      [401, 403, 400].map(() => ({
        requests: 1,
        result: { ok: false, reason: "failed" },
      })),
    );
  });

  it("requests a ticket, opens the pinned-server URL, and polls to completion", async () => {
    const calls = [];
    const responses = [
      response(200, { ticket: "one-time", login_url: "/auth/login?ticket=one-time" }),
      response(202, { status: "pending" }),
      response(200, { token: "session-jwt", user_id: "user@example.com", expires_in: 60 }),
    ];
    const electronSession = {
      fetch: async (url, init) => {
        calls.push({ url, init });
        return responses.shift();
      },
    };
    const opened = [];

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example/base",
      async (url) => opened.push(url),
      { pollIntervalMs: 1, timeoutMs: 100 },
    );

    assert.deepEqual(result, {
      ok: true,
      token: "session-jwt",
    });
    assert.deepEqual(opened, ["https://server.example/base/auth/login?ticket=one-time"]);
    assert.equal(calls[0].url, "https://server.example/base/auth/cli-login");
    assert.equal(calls[0].init.method, "POST");
    assert.equal(calls[0].init.redirect, "manual");
    assert.equal(calls[1].url, "https://server.example/base/auth/cli-poll?ticket=one-time");
    assert.equal(calls[2].url, calls[1].url);
  });

  it("keeps the organization selector on CLI and ticket login routes", async () => {
    const calls = [];
    const responses = [
      response(200, { ticket: "one-time", login_url: "/auth/login?ticket=one-time" }),
      response(200, { token: "session-jwt" }),
    ];
    const opened = [];
    const serverUrl = "https://dbc-a.cloud.databricks.com/omnigent?o=team%2Fblue";

    const result = await runOidcBrowserLogin(
      {
        fetch: async (url) => {
          calls.push(url);
          return responses.shift();
        },
      },
      serverUrl,
      async (url) => opened.push(url),
      { pollIntervalMs: 1, timeoutMs: 100 },
    );

    assert.deepEqual(result, { ok: true, token: "session-jwt" });
    assert.equal(
      calls[0],
      "https://dbc-a.cloud.databricks.com/omnigent/auth/cli-login?o=team%2Fblue",
    );
    assert.deepEqual(opened, [
      "https://dbc-a.cloud.databricks.com/omnigent/auth/login?o=team%2Fblue&ticket=one-time",
    ]);
    assert.equal(
      calls[1],
      "https://dbc-a.cloud.databricks.com/omnigent/auth/cli-poll?o=team%2Fblue&ticket=one-time",
    );
  });

  it("rejects a server-supplied external verification URL", async () => {
    const electronSession = {
      fetch: async () =>
        response(200, { ticket: "secret", login_url: "https://attacker.example/steal" }),
    };
    let opened = false;

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example",
      async () => {
        opened = true;
      },
    );

    assert.deepEqual(result, { ok: false, reason: "failed" });
    assert.equal(opened, false);
  });

  it("rejects login URLs that do not exactly bind the canonical route to the ticket", async () => {
    const invalidLoginUrls = [
      "/auth/login",
      "/auth/login?ticket=other",
      "/auth/login?ticket=secret&ticket=secret",
      "/auth/login?ticket=secret&",
      "/auth/login?%74icket=secret",
      "/auth/login?ticket=%73ecret",
      "/auth/login/../login?ticket=secret",
      "/auth/login#ticket=secret",
      "/auth/other?ticket=secret",
      "/auth/login?ticket=secret&next=%2F",
    ];

    await Promise.all(
      invalidLoginUrls.map(async (loginUrl) => {
        let fetches = 0;
        let opens = 0;
        const result = await runOidcBrowserLogin(
          {
            fetch: async () => {
              fetches += 1;
              return response(200, { ticket: "secret", login_url: loginUrl });
            },
          },
          "https://server.example/base",
          async () => {
            opens += 1;
          },
        );

        assert.deepEqual(result, { ok: false, reason: "failed" });
        assert.equal(fetches, 1);
        assert.equal(opens, 0);
      }),
    );
  });

  it("accepts the exact canonically encoded login route for a special-character ticket", async () => {
    const responses = [
      response(200, {
        ticket: "special /~!*",
        login_url: "/auth/login?ticket=special+%2F~%21%2A",
      }),
      response(200, { token: "session-jwt" }),
    ];
    const opened = [];

    const result = await runOidcBrowserLogin(
      { fetch: async () => responses.shift() },
      "https://server.example/base",
      async (url) => opened.push(url),
      { pollIntervalMs: 1, timeoutMs: 100 },
    );

    assert.deepEqual(result, { ok: true, token: "session-jwt" });
    assert.deepEqual(opened, ["https://server.example/base/auth/login?ticket=special+%2F~%21%2A"]);
  });

  it("cancels polling without redeeming the ticket", async () => {
    const controller = new AbortController();
    let fetches = 0;
    const electronSession = {
      fetch: async () => {
        fetches += 1;
        return response(200, { ticket: "secret", login_url: "/auth/login?ticket=secret" });
      },
    };

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example",
      async () => controller.abort(),
      { signal: controller.signal, pollIntervalMs: 1 },
    );

    assert.deepEqual(result, { ok: false, reason: "cancelled" });
    assert.equal(fetches, 1);
  });

  it("surfaces an expired single-use ticket", async () => {
    const responses = [
      response(200, { ticket: "expired", login_url: "/auth/login?ticket=expired" }),
      response(410, { error: "Ticket expired" }),
    ];
    const electronSession = { fetch: async () => responses.shift() };

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example",
      async () => {},
      { pollIntervalMs: 1, timeoutMs: 100 },
    );

    assert.deepEqual(result, { ok: false, reason: "expired" });
  });

  it("reports transient poll failures while continuing to completion", async () => {
    const responses = [
      response(200, { ticket: "secret", login_url: "/auth/login?ticket=secret" }),
      new Error("offline"),
      response(202, { status: "pending" }),
      response(200, { token: "session-jwt", user_id: "user@example.com", expires_in: 60 }),
    ];
    const electronSession = {
      fetch: async () => {
        const next = responses.shift();
        if (next instanceof Error) throw next;
        return next;
      },
    };
    let pollErrors = 0;

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example",
      async () => {},
      {
        pollIntervalMs: 1,
        // Generous: the outcome is decided by the scripted responses, and a
        // tight real-time budget flakes when the suite runs under load.
        timeoutMs: 5_000,
        onPollError: () => {
          pollErrors += 1;
        },
      },
    );

    assert.equal(result.ok, true);
    assert.equal(pollErrors, 1);
  });

  it("retries rate limits and transient gateway statuses", async () => {
    const responses = [
      response(200, { ticket: "secret", login_url: "/auth/login?ticket=secret" }),
      ...[429, 502, 503, 504].map((status) => response(status)),
      response(200, { token: "session-jwt" }),
    ];
    const statuses = [];
    const result = await runOidcBrowserLogin(
      { fetch: async () => responses.shift() },
      "https://server.example",
      async () => {},
      {
        pollIntervalMs: 1,
        timeoutMs: 100,
        onPollError: (status) => statuses.push(status),
      },
    );

    assert.deepEqual(result, { ok: true, token: "session-jwt" });
    assert.deepEqual(statuses, [429, 502, 503, 504]);
  });

  it("treats authorization failures as fatal ticket responses", async () => {
    const results = await Promise.all(
      [401, 403].map((status) => {
        const responses = [
          response(200, { ticket: "secret", login_url: "/auth/login?ticket=secret" }),
          response(status),
        ];
        return runOidcBrowserLogin(
          { fetch: async () => responses.shift() },
          "https://server.example",
          async () => {},
          { pollIntervalMs: 1, timeoutMs: 5_000 },
        );
      }),
    );
    assert.deepEqual(results, [
      { ok: false, reason: "failed" },
      { ok: false, reason: "failed" },
    ]);
  });

  it("bounds a pending ticket without relying on a CLI subprocess timeout", async () => {
    let fetches = 0;
    const electronSession = {
      fetch: async () => {
        fetches += 1;
        return response(200, { ticket: "pending", login_url: "/auth/login?ticket=pending" });
      },
    };

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example",
      async () => {},
      { pollIntervalMs: 10, timeoutMs: 5 },
    );

    assert.deepEqual(result, { ok: false, reason: "timed_out" });
    assert.equal(fetches, 1);
  });

  it("rejects a ticket whose body arrives past the login deadline", async () => {
    // Ticket creation is issued in time, but its response body trickles in
    // after the 5-minute window — the system browser must not open for a
    // login the shell has already abandoned.
    let opened = 0;
    const electronSession = {
      fetch: async () => ({
        status: 200,
        json: async () => {
          await new Promise((resolve) => {
            setTimeout(resolve, 150);
          });
          return { ticket: "late", login_url: "/auth/login?ticket=late" };
        },
      }),
    };

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example",
      async () => {
        opened += 1;
      },
      { pollIntervalMs: 1, timeoutMs: 100 },
    );

    assert.deepEqual(result, { ok: false, reason: "timed_out" });
    assert.equal(opened, 0);
  });

  it("rejects a token whose body arrives past the login deadline", async () => {
    // The poll request is issued in time, but the response body trickles in
    // after the 5-minute window — expired-flow output must not become a
    // session (parity with the Android shell's late-token check).
    const electronSession = {
      fetch: async (url) => {
        if (url.endsWith("/auth/cli-login")) {
          return response(200, { ticket: "late", login_url: "/auth/login?ticket=late" });
        }
        return {
          status: 200,
          json: async () => {
            await new Promise((resolve) => {
              setTimeout(resolve, 150);
            });
            return { token: "session-token" };
          },
        };
      },
    };

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example",
      async () => {},
      { pollIntervalMs: 1, timeoutMs: 100 },
    );

    assert.deepEqual(result, { ok: false, reason: "timed_out" });
  });
});

describe("OIDC session cookie installation", () => {
  function cookieSession(initialCookie, probes) {
    let stored = initialCookie ? { ...initialCookie } : null;
    const sets = [];
    const removals = [];
    const fetches = [];
    return {
      electronSession: {
        cookies: {
          get: async () => (stored ? [{ ...stored }] : []),
          set: async (details) => {
            sets.push({ ...details });
            stored = { ...details };
          },
          remove: async (url, name) => {
            removals.push({ url, name });
            stored = null;
          },
        },
        fetch: async (url) => {
          fetches.push(url);
          const next = probes.shift();
          if (next instanceof Error) throw next;
          return typeof next === "function" ? next() : next;
        },
      },
      getStored: () => stored,
      setStored: (cookie) => {
        stored = cookie ? { ...cookie } : null;
      },
      removals,
      sets,
      fetches,
    };
  }

  it("builds a valid __Host- cookie on HTTPS with no Domain", () => {
    const details = sessionCookieDetails("https://server.example", "token");
    assert.deepEqual(details, {
      url: "https://server.example",
      name: "__Host-ap_session",
      value: "token",
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      path: "/",
    });
    assert.equal(Object.hasOwn(details, "domain"), false);
  });

  it("uses the non-Host cookie on HTTP", () => {
    assert.equal(sessionCookieDetails("http://remote.example", "token").name, "ap_session");
    assert.equal(sessionCookieDetails("http://remote.example", "token").secure, false);
  });

  it("rejects a remote HTTP session before touching the cookie jar", async () => {
    const electronSession = {
      cookies: {
        get: async () => assert.fail("read cookies for remote HTTP"),
        set: async () => assert.fail("installed a remote plaintext cookie"),
        remove: async () => assert.fail("removed cookies for remote HTTP"),
      },
    };

    await assert.rejects(
      installAndVerifySessionCookie(electronSession, "http://server.example", "session-jwt"),
      /require HTTPS/,
    );
  });

  it("proves Chromium accepted the cookie and the server accepted the session", async () => {
    const state = cookieSession(null, [response(200)]);

    const result = await installAndVerifySessionCookie(
      state.electronSession,
      "https://server.example",
      "session-jwt",
    );

    assert.equal(result, undefined);
    assert.equal(state.getStored().value, "session-jwt");
    assert.deepEqual(state.removals, []);
  });

  it("preserves the organization selector during cookie verification", async () => {
    const state = cookieSession(null, [response(200)]);
    const serverUrl = "https://dbc-a.cloud.databricks.com/omnigent?o=team%2Fblue";

    await installAndVerifySessionCookie(state.electronSession, serverUrl, "session-jwt");

    assert.equal(state.getStored().url, serverUrl);
    assert.deepEqual(state.fetches, [
      "https://dbc-a.cloud.databricks.com/omnigent/v1/me?o=team%2Fblue",
    ]);
  });

  it("fails loudly when Electron silently rejects the __Host- cookie", async () => {
    const electronSession = {
      cookies: {
        set: async () => {},
        get: async () => [],
        remove: async () => {},
      },
      fetch: async () => response(200),
    };

    await assert.rejects(
      installAndVerifySessionCookie(electronSession, "https://server.example", "session-jwt"),
      /rejected the session cookie/,
    );
  });

  it("restores the prior cookie when Electron stores a cookie before rejecting the write", async () => {
    const priorCookie = {
      name: "__Host-ap_session",
      value: "prior-session",
      domain: "server.example",
      path: "/",
      httpOnly: true,
      secure: true,
      sameSite: "lax",
    };
    let stored = { ...priorCookie };
    const sets = [];
    const writeError = new Error("cookie write failed");
    const electronSession = {
      cookies: {
        get: async () => (stored ? [{ ...stored }] : []),
        set: async (details) => {
          sets.push({ ...details });
          stored = { ...details };
          if (details.value === "new-session") throw writeError;
        },
        remove: async () => {
          stored = null;
        },
      },
      fetch: async () => assert.fail("verified a rejected cookie write"),
    };

    await assert.rejects(
      installAndVerifySessionCookie(electronSession, "https://server.example", "new-session"),
      writeError,
    );

    assert.equal(stored.value, "prior-session");
    assert.deepEqual(
      sets.map(({ value }) => value),
      ["new-session", "prior-session"],
    );
  });

  it("restores the prior cookie when a rejected write leaves the cookie jar empty", async () => {
    const priorCookie = {
      name: "__Host-ap_session",
      value: "prior-session",
      domain: "server.example",
      path: "/",
      httpOnly: true,
      secure: true,
      sameSite: "lax",
    };
    let stored = { ...priorCookie };
    const sets = [];
    let removals = 0;
    const writeError = new Error("insert failed");
    const electronSession = {
      cookies: {
        get: async () => (stored ? [{ ...stored }] : []),
        set: async (details) => {
          sets.push({ ...details });
          if (details.value === "new-session") {
            stored = null;
            throw writeError;
          }
          stored = { ...details };
        },
        remove: async () => {
          removals += 1;
          stored = null;
        },
      },
      fetch: async () => assert.fail("verified a rejected cookie write"),
    };

    await assert.rejects(
      installAndVerifySessionCookie(electronSession, "https://server.example", "new-session"),
      writeError,
    );

    assert.equal(stored?.value ?? null, "prior-session");
    assert.deepEqual(
      sets.map(({ value }) => value),
      ["new-session", "prior-session"],
    );
    assert.equal(removals, 0);
  });

  it("fails when the server still reports an unauthenticated OIDC session", async () => {
    const state = cookieSession(null, [response(401, { login_url: "/auth/login" })]);

    await assert.rejects(
      installAndVerifySessionCookie(state.electronSession, "https://server.example", "session-jwt"),
      /did not accept/,
    );
    assert.equal(state.getStored(), null);
  });

  it("removes the installed cookie after transient verification exhaustion", async () => {
    const state = cookieSession(null, [response(503), response(503)]);

    await assert.rejects(
      installAndVerifySessionCookie(
        state.electronSession,
        "https://server.example",
        "session-jwt",
        { verificationAttempts: 2, retryDelayMs: 1 },
      ),
      /did not accept/,
    );

    assert.equal(state.getStored(), null);
    assert.equal(state.removals.length, 1);
  });

  it("restores the prior matching cookie after unsuccessful verification", async () => {
    const priorCookie = {
      name: "__Host-ap_session",
      value: "prior-session",
      domain: "server.example",
      path: "/",
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      expirationDate: 2000000000,
    };
    const state = cookieSession(priorCookie, [response(401, { login_url: "/auth/login" })]);

    await assert.rejects(
      installAndVerifySessionCookie(state.electronSession, "https://server.example", "new-session"),
      /did not accept/,
    );

    assert.equal(state.getStored().value, "prior-session");
    assert.equal(state.getStored().expirationDate, 2000000000);
    assert.equal(Object.hasOwn(state.getStored(), "domain"), false);
    assert.deepEqual(
      state.sets.map(({ value }) => value),
      ["new-session", "prior-session"],
    );
  });

  it("does not restore non-HttpOnly or non-string prior session cookies", async () => {
    const invalidPriorCookies = [
      {
        name: "__Host-ap_session",
        value: "script-session",
        domain: "server.example",
        path: "/",
        httpOnly: false,
        secure: true,
        sameSite: "lax",
      },
      {
        name: "__Host-ap_session",
        value: 42,
        domain: "server.example",
        path: "/",
        httpOnly: true,
        secure: true,
        sameSite: "lax",
      },
    ];

    await Promise.all(
      invalidPriorCookies.map(async (priorCookie) => {
        const state = cookieSession(priorCookie, [response(401, { login_url: "/auth/login" })]);

        await assert.rejects(
          installAndVerifySessionCookie(
            state.electronSession,
            "https://server.example",
            "new-session",
          ),
          /did not accept/,
        );

        assert.equal(state.getStored(), null);
        assert.deepEqual(
          state.sets.map(({ value }) => value),
          ["new-session"],
        );
      }),
    );
  });

  it("does not roll back a cookie replaced by another login", async () => {
    const replacement = {
      name: "__Host-ap_session",
      value: "newer-session",
      domain: "server.example",
      path: "/",
      httpOnly: true,
      secure: true,
      sameSite: "lax",
    };
    let state;
    state = cookieSession(null, [
      () => {
        state.setStored(replacement);
        return response(401, { login_url: "/auth/login" });
      },
    ]);

    await assert.rejects(
      installAndVerifySessionCookie(state.electronSession, "https://server.example", "session-jwt"),
      /did not accept/,
    );

    assert.equal(state.getStored().value, "newer-session");
    assert.deepEqual(state.removals, []);
  });

  it("serializes a newer login across an in-flight rollback", async () => {
    const priorCookie = {
      name: "__Host-ap_session",
      value: "prior-session",
      domain: "server.example",
      path: "/",
      httpOnly: true,
      secure: true,
      sameSite: "lax",
    };
    let stored = { ...priorCookie };
    const removeStarted = Promise.withResolvers();
    const finishRemove = Promise.withResolvers();
    const probes = [response(401, { login_url: "/auth/login" }), response(200)];
    const electronSession = {
      cookies: {
        get: async () => (stored ? [{ ...stored }] : []),
        set: async (details) => {
          stored = { ...details };
        },
        remove: async () => {
          removeStarted.resolve();
          await finishRemove.promise;
          stored = null;
        },
      },
      fetch: async () => probes.shift(),
    };

    const rejectedLogin = installAndVerifySessionCookie(
      electronSession,
      "https://server.example",
      "rejected-session",
    );
    await removeStarted.promise;
    const acceptedLogin = installAndVerifySessionCookie(
      electronSession,
      "https://server.example",
      "accepted-session",
    );
    await new Promise(setImmediate);
    finishRemove.resolve();

    await assert.rejects(rejectedLogin, /did not accept/);
    await acceptedLogin;
    assert.equal(stored.value, "accepted-session");
  });

  it("rolls back when cancellation wins after verification", async () => {
    const controller = new AbortController();
    const state = cookieSession(null, [response(200)]);

    await assert.rejects(
      installAndVerifySessionCookie(
        state.electronSession,
        "https://server.example",
        "session-jwt",
        {
          assertCanCommit: () => {
            controller.abort();
            throw controller.signal.reason;
          },
        },
      ),
      { name: "AbortError" },
    );

    assert.equal(state.getStored(), null);
  });

  it("retries transient verification before accepting the installed session", async () => {
    let stored = null;
    const probes = [response(503), response(429), response(200)];
    const electronSession = {
      cookies: {
        set: async (details) => {
          stored = { ...details };
        },
        get: async () => [stored],
        remove: async () => {
          stored = null;
        },
      },
      fetch: async () => probes.shift(),
    };

    await installAndVerifySessionCookie(electronSession, "https://server.example", "session-jwt", {
      retryDelayMs: 1,
    });

    assert.equal(probes.length, 0);
  });

  it("does not retry a network failure during cookie verification", async () => {
    let stored = null;
    let probes = 0;
    const electronSession = {
      cookies: {
        set: async (details) => {
          stored = { ...details };
        },
        get: async () => [stored],
        remove: async () => {
          stored = null;
        },
      },
      fetch: async () => {
        probes += 1;
        throw new Error("offline");
      },
    };

    await assert.rejects(
      installAndVerifySessionCookie(electronSession, "https://server.example", "session-jwt"),
      /offline/,
    );
    assert.equal(probes, 1);
  });

  it("cancels a transient verification delay promptly", async () => {
    const controller = new AbortController();
    const state = cookieSession(null, [response(503)]);
    const verification = installAndVerifySessionCookie(
      state.electronSession,
      "https://server.example",
      "session-jwt",
      { retryDelayMs: 50, signal: controller.signal },
    );
    controller.abort();

    await assert.rejects(verification, { name: "AbortError" });
    assert.equal(state.getStored(), null);
  });
});
