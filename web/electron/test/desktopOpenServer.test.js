// Tests for src/desktopOpenServer.js, run with `node --test` against a real
// listener on an OS-assigned loopback port.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const {
  startDesktopOpenServer,
  conversationDeepLink,
  isDesktopOpenRequest,
  DESKTOP_OPEN_PORTS,
  MIN_OPEN_INTERVAL_MS,
} = require("../src/desktopOpenServer");

const KNOWN_ORIGIN = "https://omnigent.example.com";
const CONVERSATION_ID = "e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9";

/** Start a listener that trusts only `KNOWN_ORIGIN` and records what it opens. */
async function start(t, options = {}) {
  const opened = [];
  const clock = { now: 0 };
  const server = await startDesktopOpenServer({
    isKnownOrigin: (origin) => origin === KNOWN_ORIGIN,
    openLink: (link) => {
      opened.push(link);
      return true;
    },
    ports: [0],
    now: () => clock.now,
    ...options,
  });
  t.after(() => server.close());
  return { server, opened, clock };
}

/** Send one raw request, so headers a browser sets itself (Origin, Host) can be chosen. */
function send(port, { method = "POST", route = "/open-conversation", headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        host: "127.0.0.1",
        port,
        method,
        path: route,
        headers: { Origin: KNOWN_ORIGIN, "Content-Type": "application/json", ...headers },
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () =>
          resolve({
            status: response.statusCode,
            headers: response.headers,
            body: Buffer.concat(chunks).toString("utf8"),
          }),
        );
      },
    );
    request.on("error", reject);
    request.end(body === undefined ? JSON.stringify({ conversationId: CONVERSATION_ID }) : body);
  });
}

describe("conversationDeepLink", () => {
  it("names the conversation on the page's own server", () => {
    assert.equal(
      conversationDeepLink(KNOWN_ORIGIN, CONVERSATION_ID),
      `omnigent://omnigent.example.com/c/${CONVERSATION_ID}`,
    );
    assert.equal(
      conversationDeepLink("http://localhost:8000", "conv_abc"),
      "omnigent://localhost:8000/c/conv_abc",
    );
  });

  it("refuses an id that could change the link, and a server a link can't express", () => {
    for (const id of ["", "a/b", "../x", "a?file=x", "a#b", "a b", "x".repeat(129), 7, null]) {
      assert.equal(conversationDeepLink(KNOWN_ORIGIN, id), null, String(id));
    }
    // A deep link to a remote host always means https.
    assert.equal(conversationDeepLink("http://omnigent.example.com", CONVERSATION_ID), null);
    assert.equal(conversationDeepLink("null", CONVERSATION_ID), null);
    assert.equal(conversationDeepLink(undefined, CONVERSATION_ID), null);
  });
});

describe("startDesktopOpenServer", () => {
  it("opens the conversation for a server the app knows, and tells only that page", async (t) => {
    const { server, opened } = await start(t);

    const response = await send(server.port);

    assert.equal(response.status, 204);
    assert.equal(response.body, "");
    assert.equal(response.headers["access-control-allow-origin"], KNOWN_ORIGIN);
    assert.equal(response.headers.vary, "Origin");
    // Readable by the page, which uses it to tell the app from another program on the port.
    assert.equal(response.headers["x-omnigent-desktop"], "1");
    assert.equal(response.headers["access-control-expose-headers"], "X-Omnigent-Desktop");
    assert.deepEqual(opened, [`omnigent://omnigent.example.com/c/${CONVERSATION_ID}`]);
  });

  it("tells a known server's page that the app is running, without opening anything", async (t) => {
    const { server, opened } = await start(t);

    const running = await send(server.port, { method: "GET", route: "/status" });
    assert.equal(running.status, 204);
    assert.equal(running.headers["access-control-allow-origin"], KNOWN_ORIGIN);
    assert.equal(running.body, "");
    assert.equal((await send(server.port, { route: "/status" })).status, 405);

    const unknown = await send(server.port, {
      method: "GET",
      route: "/status",
      headers: { Origin: "https://attacker.example" },
    });
    assert.equal(unknown.status, 403);
    assert.equal(unknown.headers["access-control-allow-origin"], undefined);
    assert.deepEqual(opened, []);
  });

  it("answers the browser's preflight for a known server without opening anything", async (t) => {
    const { server, opened } = await start(t);

    const response = await send(server.port, {
      method: "OPTIONS",
      headers: {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Private-Network": "true",
      },
    });

    assert.equal(response.status, 204);
    assert.equal(response.headers["access-control-allow-origin"], KNOWN_ORIGIN);
    assert.equal(response.headers["access-control-allow-methods"], "GET, POST");
    assert.equal(response.headers["access-control-allow-headers"], "Content-Type");
    assert.equal(response.headers["access-control-allow-private-network"], "true");
    assert.deepEqual(opened, []);
  });

  it("refuses every other caller without opening or revealing anything", async (t) => {
    const { server, opened } = await start(t);
    const refused = [
      ["an unknown site", { headers: { Origin: "https://attacker.example" } }],
      ["an opaque origin", { headers: { Origin: "null" } }],
      [
        "a preflight from an unknown site",
        { method: "OPTIONS", headers: { Origin: "https://attacker.example" } },
      ],
      // DNS rebinding: the page reaches loopback through a name it controls.
      ["a rebound host name", { headers: { Host: `attacker.example:${server.port}` } }],
    ];
    for (const [label, request] of refused) {
      // oxlint-disable-next-line no-await-in-loop -- Sequential by design: one caller at a time.
      const response = await send(server.port, request);
      assert.equal(response.status, 403, label);
      assert.equal(response.headers["access-control-allow-origin"], undefined, label);
      assert.equal(response.body, "", label);
    }
    // A request with no Origin at all: not from a page, so there is no server to open.
    const noOrigin = await new Promise((resolve, reject) => {
      const request = http.request(
        { host: "127.0.0.1", port: server.port, method: "POST", path: "/open-conversation" },
        (response) => {
          response.resume();
          response.on("end", () => resolve(response.statusCode));
        },
      );
      request.on("error", reject);
      request.end(JSON.stringify({ conversationId: CONVERSATION_ID }));
    });
    assert.equal(noOrigin, 403);
    assert.deepEqual(opened, []);
  });

  it("doesn't offer itself to a known server that no deep link can name", async (t) => {
    const plainHttp = "http://omnigent.example.com";
    const { server, opened } = await start(t, { isKnownOrigin: () => true });

    for (const request of [{ method: "GET", route: "/status" }, {}]) {
      // oxlint-disable-next-line no-await-in-loop -- Sequential by design: one request at a time.
      const response = await send(server.port, { ...request, headers: { Origin: plainHttp } });
      assert.equal(response.status, 403);
      assert.equal(response.headers["access-control-allow-origin"], undefined);
    }
    assert.deepEqual(opened, []);
  });

  it("rejects a malformed request from a known server", async (t) => {
    const { server, opened } = await start(t);
    const cases = [
      [404, { route: "/other" }],
      [405, { method: "GET" }],
      [400, { body: "not json" }],
      [400, { body: JSON.stringify({ conversationId: "../settings" }) }],
      [400, { body: JSON.stringify({}) }],
      [413, { body: JSON.stringify({ conversationId: "x".repeat(2000) }) }],
    ];
    for (const [status, request] of cases) {
      // oxlint-disable-next-line no-await-in-loop -- Sequential by design: one request at a time.
      const response = await send(server.port, request);
      assert.equal(response.status, status, JSON.stringify(request));
    }
    assert.deepEqual(opened, []);
  });

  it("answers only once the app shows the conversation, and says when it could not", async (t) => {
    let finish;
    let opening;
    const started = new Promise((resolve) => {
      opening = resolve;
    });
    const { server } = await start(t, {
      openLink: () =>
        new Promise((resolve) => {
          finish = resolve;
          opening();
        }),
    });

    let answered = false;
    const pending = send(server.port).then((response) => {
      answered = true;
      return response;
    });
    await started;
    // Give an early answer the chance to arrive: none may.
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    assert.equal(answered, false);
    finish(true);
    assert.equal((await pending).status, 204);

    const failing = await start(t, { openLink: async () => false });
    const refused = await send(failing.server.port);
    assert.equal(refused.status, 504);
    // Still the app's own answer, so the page doesn't go looking on another port.
    assert.equal(refused.headers["x-omnigent-desktop"], "1");
  });

  it("limits how often a page can raise the app", async (t) => {
    const { server, opened, clock } = await start(t);

    assert.equal((await send(server.port)).status, 204);
    clock.now = MIN_OPEN_INTERVAL_MS - 1;
    assert.equal((await send(server.port)).status, 429);
    clock.now = MIN_OPEN_INTERVAL_MS;
    assert.equal((await send(server.port)).status, 204);

    assert.equal(opened.length, 2);
  });

  it("moves to the next port when one is taken, and reports when all are", async (t) => {
    const blocker = http.createServer();
    await new Promise((resolve) => {
      blocker.listen({ port: 0, host: "127.0.0.1" }, resolve);
    });
    t.after(() => blocker.close());
    const taken = blocker.address().port;

    const { server } = await start(t, { ports: [taken, 0] });
    assert.notEqual(server.port, taken);
    assert.equal((await send(server.port)).status, 204);

    assert.equal(
      await startDesktopOpenServer({
        isKnownOrigin: () => true,
        openLink: () => true,
        ports: [taken],
      }),
      null,
    );
  });

  it("listens on the ports the web page probes", () => {
    const page = fs.readFileSync(
      path.join(__dirname, "../../src/lib/desktopAppHandoff.ts"),
      "utf8",
    );
    const probed = page
      .match(/DESKTOP_OPEN_PORTS = \[([^\]]*)\]/)[1]
      .split(",")
      .map(Number);
    assert.deepEqual(DESKTOP_OPEN_PORTS, probed);
    assert.ok(DESKTOP_OPEN_PORTS.every((port) => Number.isInteger(port) && port > 1024));
  });

  it("recognizes a request for the listener on every port it may hold", () => {
    for (const port of DESKTOP_OPEN_PORTS) {
      assert.equal(isDesktopOpenRequest(`http://127.0.0.1:${port}/open-conversation`), true);
      assert.equal(isDesktopOpenRequest(`http://127.0.0.1:${port}/status`), true);
    }
    assert.equal(isDesktopOpenRequest("http://127.0.0.1:5173/c/abc"), false);
    assert.equal(isDesktopOpenRequest(`https://example.com:${DESKTOP_OPEN_PORTS[0]}/`), false);
    assert.equal(isDesktopOpenRequest("not a url"), false);
  });
});
