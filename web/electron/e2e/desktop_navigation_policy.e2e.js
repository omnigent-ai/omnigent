"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const {
  desktopDepsAvailable,
  launchDesktop,
  saveRecording,
  spawnServer,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();

it(
  "attributes repeated document responses and cancels internal responses",
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
    const fixture = http.createServer((req, res) => {
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
        isArcaAgentContext: (context) => context?.trustedPreviewFixture === true,
      });
      const response = registry.shouldCancelAgentResponse.bind(registry);
      registry.shouldCancelAgentResponse = async (details) => {
        const cancel = await response(details);
        global.navigationResponses.push({ url: details.url, ip: details.ip,
          webContentsId: details.webContentsId, statusCode: details.statusCode, cancel });
        return cancel;
      };
      const request = registry.shouldCancelAgentRequest.bind(registry);
      registry.shouldCancelAgentRequest = async (details) => {
        const cancel = registry.get("response-fixture")?.view.webContents.id === details.webContentsId
          && details.url === ${JSON.stringify(localUrl)} ? false : await request(details);
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
            entry.agentNavLocked = true;
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

      const cancellation = await electronApp.evaluate(async (_electron, url) => {
        const registry = global.omnigentTestBrowserRegistries[0];
        registry.openOrNavigate("response-fixture", null);
        const entry = registry.get("response-fixture");
        entry.agentNavLocked = true;
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
