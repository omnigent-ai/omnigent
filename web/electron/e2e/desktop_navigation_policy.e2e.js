"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const { createHash } = require("node:crypto");
const {
  desktopDepsAvailable,
  launchDesktop,
  saveRecording,
  spawnServer,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();

it(
  "guards documents, subresources, WebSockets, and worker network requests",
  {
    skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`,
    timeout: 180_000,
  },
  async () => {
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "omni-navigation-"));
    const recordDir =
      process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
      fs.mkdtempSync(path.join(os.tmpdir(), "omni-navigation-recordings-"));
    const evidence = {};
    const privateRequests = [];
    const fixture = http.createServer((req, res) => {
      if (req.url.startsWith("/private")) privateRequests.push(req.url);
      if (req.url === "/policy-worker.js") {
        res.setHeader("Content-Type", "application/javascript");
        res.end(
          "self.onmessage = (event) => { fetch(event.data).catch(() => {}); }; self.skipWaiting();",
        );
        return;
      }
      if (req.url === "/redirect") {
        res.writeHead(302, {
          Location: `http://lvh.me:${fixture.address().port}/embedded?kind=redirect`,
        });
        res.end();
        return;
      }
      res.setHeader("Cache-Control", "public, max-age=3600");
      res.setHeader("ETag", '"preview-v1"');
      if (req.headers["if-none-match"] === '"preview-v1"') {
        res.writeHead(304).end();
      } else {
        res.setHeader("Content-Type", "text/html");
        res.end(
          "<h1>Local preview loaded</h1><p>Trusted preview fixture in a real Electron browser view.</p>",
        );
      }
    });
    await new Promise((resolve) => {
      fixture.listen(0, "127.0.0.1", resolve);
    });
    const localUrl = `http://localhost:${fixture.address().port}/`;
    const preload = path.join(scratch, "observe-network.cjs");
    const modulePath = path.resolve(__dirname, "../src/browserViewRegistry.js");
    // Main-only fault injection isolates the response guard from preflight.
    // No renderer gains an exception or a way to manufacture an Arca identity.
    fs.writeFileSync(
      preload,
      `
    const mod = require(${JSON.stringify(modulePath)});
    const create = mod.createBrowserViewRegistry;
    global.navigationRequests = [];
    global.navigationResponses = [];
    global.navigationRedirects = [];
    mod.createBrowserViewRegistry = (opts) => {
      const registry = create({ ...opts,
        allowsLocalhost: (context) => context?.trustedPreviewFixture === true,
      });
      const response = registry.shouldCancelAgentResponse.bind(registry);
      registry.shouldCancelAgentResponse = async (details, partition) => {
        const cancel = await response(details, partition);
        global.navigationResponses.push({ url: details.url, ip: details.ip,
          webContentsId: details.webContentsId, statusCode: details.statusCode, cancel });
        return cancel;
      };
      const request = registry.shouldCancelAgentRequest.bind(registry);
      registry.shouldCancelAgentRequest = async (details, partition) => {
        const cancel = registry.get("response-fixture")?.view.webContents.id === details.webContentsId
          && details.url === ${JSON.stringify(localUrl)} ? false : await request(details, partition);
        global.navigationRequests.push({ url: details.url, resourceType: details.resourceType,
          webContentsId: details.webContentsId, cancel });
        return cancel;
      };
      return registry;
    };
  `,
    );
    let server;
    let desktop;
    try {
      server = await spawnServer(scratch);
      desktop = await launchDesktop({ serverUrl: server.serverUrl, recordDir, preload: [preload] });
      const { electronApp, window } = desktop;
      evidence.electronVersion = await electronApp.evaluate(() => process.versions.electron);
      await window.getByText("What should we build?").waitFor({ timeout: 30_000 });
      const phase = async (message) => {
        console.log(message);
        await window.evaluate((text) => {
          let banner = document.getElementById("navigation-verification");
          if (!banner) {
            banner = document.createElement("div");
            banner.id = "navigation-verification";
            banner.style.cssText =
              "position:fixed;inset:55px 20px auto;z-index:99999;background:#173b61;color:white;padding:18px;font:20px system-ui";
            document.body.appendChild(banner);
          }
          banner.textContent = text;
        }, message);
        await window.waitForTimeout(2500);
      };
      const navigate = (id, url, preview = false) =>
        electronApp.evaluate(
          async (_electron, args) => {
            const registry = global.omnigentTestBrowserRegistries[0];
            const context = args.preview ? { trustedPreviewFixture: true } : {};
            const verdict = await registry.checkAgentNavigation(args.id, args.url, context);
            if (!verdict.ok) return verdict;
            const opened = registry.openOrNavigate(args.id, null, {
              x: 40,
              y: 160,
              width: 900,
              height: 440,
            });
            if (!opened.ok) return { ok: false, error: opened.error };
            const entry = registry.get(args.id);
            entry.navigationPolicyEnforced = true;
            entry.agentContext = context;
            registry.setActive(args.id);
            if (args.id === "hsts-fixture") {
              entry.view.webContents.session.webRequest.onBeforeRedirect(
                { urls: ["*://hsts.badssl.com/*"] },
                ({ url: sourceUrl, redirectURL, statusCode, statusLine, responseHeaders, ip }) => {
                  global.navigationRedirects.push({
                    url: sourceUrl,
                    redirectURL,
                    statusCode,
                    statusLine,
                    responseHeaders,
                    ip,
                  });
                },
              );
            }
            await Promise.race([
              entry.view.webContents.loadURL(args.url),
              new Promise((_, reject) => {
                setTimeout(
                  () =>
                    reject(
                      new Error(
                        "Navigation deadline: " + JSON.stringify(global.navigationResponses),
                      ),
                    ),
                  15000,
                );
              }),
            ]);
            return {
              ok: true,
              id: entry.view.webContents.id,
              title: entry.view.webContents.getTitle(),
              url: entry.view.webContents.getURL(),
            };
          },
          { id, url, preview },
        );

      await phase("1. Public navigation through the production document guards");
      const publicPage = await navigate("public-fixture", "https://example.com/");
      evidence.publicPage = publicPage;
      assert.equal(publicPage.ok, true, publicPage.error);
      assert.match(publicPage.title, /Example Domain/);
      await phase("Public page loaded. Repeating the navigation checks the cache path.");
      assert.equal((await navigate("public-fixture", "https://example.com/")).ok, true);

      await phase("HSTS: learn the HTTPS policy, then navigate to the HTTP URL.");
      evidence.hstsSecure = await navigate("hsts-fixture", "https://hsts.badssl.com/");
      assert.equal(evidence.hstsSecure.ok, true, evidence.hstsSecure.error);
      evidence.hstsUpgrade = await navigate("hsts-fixture", "http://hsts.badssl.com/");
      assert.equal(evidence.hstsUpgrade.ok, true, evidence.hstsUpgrade.error);
      assert.equal(evidence.hstsUpgrade.url, "https://hsts.badssl.com/");
      const redirects = await electronApp.evaluate(() => global.navigationRedirects);
      assert.ok(
        redirects.some(
          (event) =>
            event.url === "http://hsts.badssl.com/" &&
            event.redirectURL === "https://hsts.badssl.com/" &&
            event.statusCode === 307 &&
            Object.entries(event.responseHeaders).some(
              ([name, values]) =>
                name.toLowerCase() === "non-authoritative-reason" && values.includes("HSTS"),
            ),
        ),
        JSON.stringify(redirects),
      );

      const denied = await navigate("alias-fixture", `http://lvh.me:${fixture.address().port}/`);
      evidence.denied = denied;
      assert.equal(denied.ok, false);
      assert.match(denied.error, /navigation blocked/);
      await phase("2. Loopback alias refused: navigation blocked");

      const preview = await navigate("preview-fixture", localUrl, true);
      evidence.preview = preview;
      assert.equal(preview.ok, true, preview.error);
      await phase(
        "3. Trusted localhost preview loaded. Fetching the document again with the cache guard.",
      );
      assert.equal((await navigate("preview-fixture", localUrl, true)).ok, true);

      await phase("Embedded documents: refuse internal iframe and object destinations.");
      const embeddedUrl = `http://lvh.me:${fixture.address().port}/embedded`;
      evidence.embedded = await electronApp.evaluate(
        async (_electron, { url, previewUrl }) => {
          const entry = global.omnigentTestBrowserRegistries[0].get("preview-fixture");
          await entry.view.webContents.executeJavaScript(`
          for (const tag of ["iframe", "object"]) {
            const element = document.createElement(tag);
            if (tag === "object") {
              element.type = "text/html";
              element.data = ${JSON.stringify(url)} + "?kind=" + tag;
            } else {
              element.src = ${JSON.stringify(url)} + "?kind=" + tag;
            }
            document.body.appendChild(element);
          }
          void 0;
        `);
          const waitForRequests = (count) => {
            const deadline = Date.now() + 10000;
            return new Promise((resolve) => {
              const check = () => {
                const requests = global.navigationRequests.filter((event) =>
                  event.url.startsWith(url),
                );
                if (requests.length >= count || Date.now() >= deadline) {
                  resolve(requests);
                } else {
                  setTimeout(check, 50);
                }
              };
              check();
            });
          };
          await waitForRequests(2);
          await entry.view.webContents.executeJavaScript(
            "location.href = " + JSON.stringify(previewUrl + "redirect") + "; void 0;",
          );
          await waitForRequests(3);
          await entry.view.webContents.executeJavaScript(
            "window.open(" + JSON.stringify(url + "?kind=window-open") + "); void 0;",
          );
          return waitForRequests(4);
        },
        { url: embeddedUrl, previewUrl: localUrl },
      );
      for (const [tag, resourceType] of [
        ["iframe", "subFrame"],
        ["object", "object"],
        ["redirect", "mainFrame"],
        ["window-open", "mainFrame"],
      ]) {
        assert.ok(
          evidence.embedded.some(
            (event) =>
              event.url === `${embeddedUrl}?kind=${tag}` &&
              event.resourceType === resourceType &&
              event.webContentsId === preview.id &&
              event.cancel,
          ),
          JSON.stringify(evidence.embedded),
        );
      }

      await phase("Subresources and workers: private requests cancel without closing the view.");
      assert.equal((await navigate("preview-fixture", localUrl, true)).ok, true);
      const privateProbeIds = [
        "fetch",
        "private-name-fetch",
        "private-name-head",
        "xhr",
        "image",
        "websocket",
        "private-name-websocket",
        "dedicated-worker",
        "shared-worker",
        "service-worker",
      ];
      evidence.subresources = await electronApp.evaluate(
        async (_electron, { port, privateProbeIds: deniedProbeIds }) => {
          const entry = global.omnigentTestBrowserRegistries[0].get("preview-fixture");
          // Install a same-origin service worker while this preview has loopback access.
          await entry.view.webContents.executeJavaScript(`
          (async () => {
            const registration = await navigator.serviceWorker.register("/policy-worker.js");
            await navigator.serviceWorker.ready;
            window.policyServiceWorker = registration.active;
          })()
        `);
          entry.agentContext = {};
          const privateUrl = "http://127.0.0.1:" + port + "/private";
          await entry.view.webContents.executeJavaScript(`
          void fetch(${JSON.stringify(privateUrl + "?kind=fetch")}).catch(() => {});
          void fetch("http://lvh.me:" + ${port} + "/private?kind=private-name-fetch").catch(() => {});
          void fetch("http://lvh.me:" + ${port} + "/private?kind=private-name-head", { method: "HEAD" }).catch(() => {});
          const xhr = new XMLHttpRequest();
          xhr.open("GET", ${JSON.stringify(privateUrl + "?kind=xhr")}); xhr.send();
          const image = new Image();
          image.src = "http://169.254.169.254/?kind=image";
          document.body.appendChild(image);
          const socket = new WebSocket("ws://localhost:" + ${port} + "/?kind=websocket");
          socket.onerror = () => {};
          const privateNameSocket = new WebSocket("ws://lvh.me:" + ${port} + "/?kind=private-name-websocket");
          privateNameSocket.onerror = () => {};
          const workerCode = "fetch(" + ${JSON.stringify(JSON.stringify(privateUrl + "?kind=dedicated-worker"))} + ").catch(() => {});";
          window.policyDedicatedWorker = new Worker(URL.createObjectURL(new Blob([workerCode], { type: "application/javascript" })));
          const sharedCode = "onconnect = () => { fetch(" + ${JSON.stringify(JSON.stringify(privateUrl + "?kind=shared-worker"))} + ").catch(() => {}); };";
          window.policySharedWorker = new SharedWorker(URL.createObjectURL(new Blob([sharedCode], { type: "application/javascript" })));
          window.policyServiceWorker.postMessage(${JSON.stringify(privateUrl + "?kind=service-worker")});
          void fetch("https://example.com/?kind=public-subresource", { mode: "no-cors" }).catch(() => {});
          void 0;
        `);
          const kinds = [...deniedProbeIds, "public-subresource"];
          const deadline = Date.now() + 10000;
          await new Promise((resolve) => {
            const check = () => {
              const observed = kinds.every((kind) =>
                global.navigationRequests.some((request) => request.url.endsWith("?kind=" + kind)),
              );
              if (observed || Date.now() >= deadline) resolve();
              else setTimeout(check, 50);
            };
            check();
          });
          const requests = global.navigationRequests.filter((request) =>
            kinds.some((kind) => request.url.endsWith("?kind=" + kind)),
          );
          const alive = !entry.view.webContents.isDestroyed();
          const registeredViewIds = [];
          global.omnigentTestBrowserRegistries[0].forEach((view) => {
            registeredViewIds.push(view.view.webContents.id);
          });
          entry.agentContext = { trustedPreviewFixture: true };
          await entry.view.webContents.executeJavaScript(
            "navigator.serviceWorker.getRegistrations().then((registrations) => Promise.all(registrations.map((registration) => registration.unregister())))",
          );
          return { requests, alive, registeredViewIds };
        },
        { port: fixture.address().port, privateProbeIds },
      );
      for (const kind of privateProbeIds) {
        assert.ok(
          evidence.subresources.requests.some(
            (request) => request.url.endsWith("?kind=" + kind) && request.cancel,
          ),
          JSON.stringify(evidence.subresources),
        );
      }
      const serviceWorker = evidence.subresources.requests.find((request) =>
        request.url.endsWith("?kind=service-worker"),
      );
      assert.equal(
        evidence.subresources.registeredViewIds.includes(serviceWorker.webContentsId),
        false,
        "the service-worker request uses partition fallback, not a registered view",
      );
      assert.deepEqual(
        privateRequests,
        [],
        "private requests are cancelled before reaching the server",
      );
      assert.ok(
        evidence.subresources.requests.some(
          (request) => request.url.endsWith("?kind=public-subresource") && !request.cancel,
        ),
        JSON.stringify(evidence.subresources),
      );
      assert.equal(evidence.subresources.alive, true);

      const cancellation = await electronApp.evaluate(async (_electron, url) => {
        const registry = global.omnigentTestBrowserRegistries[0];
        registry.openOrNavigate("response-fixture", null);
        const entry = registry.get("response-fixture");
        entry.navigationPolicyEnforced = true;
        entry.agentContext = {};
        try {
          await Promise.race([
            entry.view.webContents.loadURL(url),
            new Promise((_, reject) => {
              setTimeout(() => reject(new Error("response wait elapsed")), 10000);
            }),
          ]);
          return { cancelled: false };
        } catch (error) {
          return { cancelled: true, error: String(error), id: entry.view.webContents.id };
        }
      }, localUrl);
      evidence.cancellation = cancellation;
      assert.equal(cancellation.cancelled, true);
      const responses = await electronApp.evaluate(() => global.navigationResponses);
      for (const [id, url] of [
        [publicPage.id, "https://example.com/"],
        [preview.id, localUrl],
      ]) {
        const events = responses.filter((event) => event.webContentsId === id && event.url === url);
        assert.ok(events.length >= 2, JSON.stringify(responses));
        assert.ok(
          events.every((event) => event.ip && !event.cancel),
          JSON.stringify(events),
        );
      }
      assert.ok(
        responses
          .filter((event) => event.webContentsId === preview.id && event.url === localUrl)
          .every((event) => event.statusCode === 200),
        JSON.stringify(responses),
      );
      assert.ok(
        responses.some(
          (event) => event.webContentsId === cancellation.id && event.ip && event.cancel,
        ),
      );
      await phase("Verified: public pages load, internal responses cancel, trusted previews work.");
    } finally {
      if (desktop) {
        try {
          Object.assign(
            evidence,
            await desktop.electronApp.evaluate(() => ({
              requests: global.navigationRequests,
              responses: global.navigationResponses,
              redirects: global.navigationRedirects,
            })),
          );
        } catch (error) {
          evidence.captureError = String(error);
        }
      }
      fs.mkdirSync(recordDir, { recursive: true });
      fs.writeFileSync(
        path.join(recordDir, "network-evidence.json"),
        JSON.stringify(evidence, null, 2),
      );
      try {
        if (desktop) {
          await desktop.electronApp.close();
          await desktop.stopDisplayCapture();
          console.log(saveRecording(recordDir, "navigation-policy"));
          fs.rmSync(desktop.userDataDir, { recursive: true, force: true });
        }
      } finally {
        if (server) await server.close();
        fixture.closeAllConnections();
        await new Promise((resolve) => {
          fixture.close(resolve);
        });
        fs.rmSync(scratch, { recursive: true, force: true });
      }
    }
  },
);

it(
  "opens this machine's loopback preview via the address bar and agent relay, denying a remote session",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`, timeout: 120_000 },
  async () => {
    const { startDesignBackend, eventually } = require("./desktopDesignPromptHarness");
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "omni-localhost-"));
    const recordDir =
      process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
      fs.mkdtempSync(path.join(os.tmpdir(), "omni-localhost-recordings-"));
    let previewLoads = 0;
    const fixture = http.createServer((request, response) => {
      if (request.url === "/") previewLoads++;
      response.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" });
      response.end("<title>This machine preview</title><h1>Localhost preview works</h1>");
    });
    await new Promise((resolve) => {
      fixture.listen(0, "127.0.0.1", resolve);
    });
    const previewUrl = `http://localhost:${fixture.address().port}/`;
    const corsRequests = [];
    const corsFixture = http.createServer((request, response) => {
      corsRequests.push({ method: request.method, headers: request.headers });
      response.setHeader("Access-Control-Allow-Origin", "*");
      response.setHeader("Access-Control-Allow-Methods", "GET, POST");
      response.setHeader("Access-Control-Allow-Headers", "content-type");
      response.setHeader("Cache-Control", "public, max-age=3600");
      if (request.method === "OPTIONS") response.writeHead(204).end();
      else response.end("cors-ok");
    });
    corsFixture.on("upgrade", (request, socket) => {
      const accept = createHash("sha1")
        .update(request.headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
        .digest("base64");
      socket.write(
        `HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`,
      );
      let buffered = Buffer.alloc(0);
      // This fixture echoes one short masked text frame, then closes the socket.
      socket.on("data", (chunk) => {
        buffered = Buffer.concat([buffered, chunk]);
        if (buffered.length < 2) return;
        const length = buffered[1] & 0x7f;
        if (buffered[0] !== 0x81 || !(buffered[1] & 0x80) || length > 125) {
          socket.destroy();
          return;
        }
        if (buffered.length < length + 6) return;
        const payload = Buffer.from(buffered.subarray(6, length + 6));
        for (let index = 0; index < length; index++) payload[index] ^= buffered[2 + (index % 4)];
        socket.end(Buffer.concat([Buffer.from([0x81, length]), payload]));
      });
      socket.on("error", () => {});
    });
    await new Promise((resolve) => {
      corsFixture.listen(0, "127.0.0.1", resolve);
    });
    const corsUrl = `http://127.0.0.1:${corsFixture.address().port}/allowed-cors`;
    const socketUrl = `ws://127.0.0.1:${corsFixture.address().port}/echo`;
    const observeAllowed = path.join(scratch, "observe-allowed.cjs");
    fs.writeFileSync(
      observeAllowed,
      `
      const mod = require(${JSON.stringify(path.resolve(__dirname, "../src/browserViewRegistry.js"))});
      const create = mod.createBrowserViewRegistry;
      global.allowedNetworkResponses = [];
      mod.createBrowserViewRegistry = (opts) => {
        const registry = create(opts);
        const response = registry.shouldCancelAgentResponse.bind(registry);
        registry.shouldCancelAgentResponse = async (details, partition) => {
          const cancel = await response(details, partition);
          global.allowedNetworkResponses.push({ url: details.url, ip: details.ip,
            resourceType: details.resourceType, statusCode: details.statusCode, cancel });
          return cancel;
        };
        return registry;
      };
    `,
    );
    const hostId = "a".repeat(32);
    let backend;
    let desktop;
    try {
      backend = await startDesignBackend(scratch);
      const machineHome = path.join(scratch, "machine");
      const stateDir = path.join(machineHome, ".omnigent");
      fs.mkdirSync(path.join(stateDir, "daemons"), { recursive: true });
      fs.writeFileSync(path.join(stateDir, "config.yaml"), `host:\n  host_id: ${hostId}\n`);
      // Disk-backed enrollment and a live PID exercise the production predicate.
      fs.writeFileSync(
        path.join(stateDir, "daemons", "local.json"),
        JSON.stringify({
          pid: process.pid,
          target: backend.serverUrl,
          server_url: backend.serverUrl,
          mode: "server",
          host_id: hostId,
        }),
      );
      desktop = await launchDesktop({
        serverUrl: backend.serverUrl,
        recordDir,
        env: { HOME: machineHome, OMNIGENT_CONFIG_HOME: stateDir },
        preload: [observeAllowed],
      });
      const { window, electronApp } = desktop;
      let sessionIsLocal = true;
      await window.route(/\/v1\/sessions\/[a-f0-9]+(?:\?.*)?$/, async (route) => {
        if (route.request().method() !== "GET") return route.continue();
        const response = await route.fetch();
        const body = await response.json();
        const local = body.id === backend.sessionId && sessionIsLocal;
        await route.fulfill({
          response,
          json: { ...body, host_id: local ? hostId : "b".repeat(32) },
        });
      });
      await window.goto(`${backend.serverUrl}/c/${backend.sessionId}`);
      await window.getByRole("button", { name: /^(Expand|Collapse) right panel$/ }).waitFor();
      const expand = window.getByRole("button", { name: "Expand right panel", exact: true });
      if (await expand.isVisible()) await expand.click();
      await window.getByRole("button", { name: "Open new", exact: true }).click();
      await window.getByRole("menuitem", { name: "Browser", exact: true }).click();
      const bar = window.getByRole("textbox", { name: "Address bar" });
      await bar.fill(previewUrl);
      await bar.press("Enter");
      await eventually(
        () =>
          electronApp.evaluate((_electron, url) => {
            const registry = global.omnigentTestBrowserRegistries[0];
            const entry = registry.get(registry.activeConversationId());
            return (
              entry?.view.webContents.getURL() === url &&
              entry.agentContext?.hostIsThisMachine === true
            );
          }, previewUrl),
        "address-bar loopback preview",
      );
      const action = async (sessionId) => {
        const response = await fetch(
          `${backend.serverUrl}/v1/sessions/${sessionId}/browser/action_request`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "navigate", args: { url: previewUrl } }),
            signal: AbortSignal.timeout(30_000),
          },
        );
        assert.equal(response.ok, true);
        return response.json();
      };
      assert.equal((await action(backend.sessionId)).ok, true);
      const content = await electronApp.evaluate(async (_electron, sessionId) => {
        const entry = global.omnigentTestBrowserRegistries[0].get(sessionId);
        await new Promise((resolve) => {
          if (!entry.view.webContents.isLoading()) resolve();
          else entry.view.webContents.once("did-finish-load", resolve);
        });
        return entry.view.webContents.executeJavaScript("document.body.innerText");
      }, backend.sessionId);
      assert.match(content, /Localhost preview works/);
      const allowed = await electronApp.evaluate(
        async (_electron, { sessionId, corsUrl: requestUrl, socketUrl: echoUrl }) => {
          const entry = global.omnigentTestBrowserRegistries[0].get(sessionId);
          return entry.view.webContents.executeJavaScript(`
          (async () => {
            const get = await fetch(${JSON.stringify(requestUrl)} + "?method=get", { mode: "cors" });
            const getText = await get.text();
            const repeatedGet = await fetch(${JSON.stringify(requestUrl)} + "?method=get", { mode: "cors" });
            const repeatedGetText = await repeatedGet.text();
            const post = await fetch(${JSON.stringify(requestUrl)} + "?method=post", {
              mode: "cors", method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
            });
            const postText = await post.text();
            const echo = await new Promise((resolve, reject) => {
              const socket = new WebSocket(${JSON.stringify(echoUrl)});
              const timeout = setTimeout(() => { socket.close(); reject(new Error("echo timed out")); }, 10000);
              socket.onopen = () => socket.send("allowed-echo");
              socket.onmessage = (event) => { clearTimeout(timeout); resolve(event.data); socket.close(); };
              socket.onerror = () => { clearTimeout(timeout); reject(new Error("echo failed")); };
            });
            return { getText, repeatedGetText, postText, echo };
          })()
        `);
        },
        { sessionId: backend.sessionId, corsUrl, socketUrl },
      );
      assert.deepEqual(allowed, {
        getText: "cors-ok",
        repeatedGetText: "cors-ok",
        postText: "cors-ok",
        echo: "allowed-echo",
      });
      assert.equal(
        corsRequests.filter((request) => request.method === "OPTIONS").length,
        1,
        "only the JSON POST needs a CORS preflight",
      );
      const preflight = corsRequests.find((request) => request.method === "OPTIONS");
      assert.equal(
        preflight.headers["access-control-request-headers"],
        "content-type",
        "main-process Cache-Control edits do not add a CORS request header",
      );
      const responses = await electronApp.evaluate(() => global.allowedNetworkResponses);
      assert.equal(
        corsRequests.filter((request) => request.method === "GET").length,
        2,
        "cacheable CORS responses still reach the server twice",
      );
      for (const url of [corsUrl + "?method=get", corsUrl + "?method=post"]) {
        assert.ok(
          responses.some(
            (response) =>
              response.url === url &&
              response.ip === "127.0.0.1" &&
              response.statusCode === 200 &&
              !response.cancel,
          ),
          JSON.stringify({ url, responses }),
        );
      }
      assert.ok(
        responses.some(
          (response) =>
            response.url === socketUrl && response.statusCode === 101 && !response.cancel,
        ),
        "eligible loopback WebSocket upgrade completes",
      );
      const getResponses = responses.filter((response) => response.url === corsUrl + "?method=get");
      assert.equal(getResponses.length, 2, "both cacheable fetches complete the response guard");
      assert.ok(
        getResponses.every(
          (response) =>
            response.ip === "127.0.0.1" && response.statusCode === 200 && !response.cancel,
        ),
        JSON.stringify(getResponses),
      );
      fs.mkdirSync(recordDir, { recursive: true });
      fs.writeFileSync(
        path.join(recordDir, "allowed-network-evidence.json"),
        JSON.stringify({ allowed, corsRequests, responses }, null, 2),
      );
      const loadsBeforeMove = previewLoads;
      sessionIsLocal = false;
      const moved = await action(backend.sessionId);
      assert.equal(moved.ok, false, JSON.stringify(moved));
      assert.match(moved.error, /blocked/i);
      assert.equal(previewLoads, loadsBeforeMove, "a moved session sends no loopback request");
      assert.equal(
        await electronApp.evaluate((_electron, sessionId) => {
          return global.omnigentTestBrowserRegistries[0].get(sessionId).view.webContents.getURL();
        }, backend.sessionId),
        "about:blank",
        "a moved session removes the previous local document",
      );
      await window.goto(`${backend.serverUrl}/c/${backend.otherSessionId}`);
      await window.getByRole("button", { name: /^(Expand|Collapse) right panel$/ }).waitFor();
      const denied = await eventually(async () => {
        const result = await action(backend.otherSessionId);
        return result.error === "no browser renderer is connected" ? null : result;
      }, "remote session browser relay");
      assert.equal(denied.ok, false, JSON.stringify(denied));
      assert.match(denied.error, /blocked/i);
    } finally {
      if (desktop) {
        await desktop.window.unrouteAll({ behavior: "wait" });
        await desktop.electronApp.close();
        await desktop.stopDisplayCapture();
        console.log(saveRecording(recordDir, "this-machine-localhost"));
        fs.rmSync(desktop.userDataDir, { recursive: true, force: true });
      }
      if (backend) await backend.close();
      corsFixture.closeAllConnections();
      await new Promise((resolve) => {
        corsFixture.close(resolve);
      });
      fixture.closeAllConnections();
      await new Promise((resolve) => {
        fixture.close(resolve);
      });
      fs.rmSync(scratch, { recursive: true, force: true });
    }
  },
);
