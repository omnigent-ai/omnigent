"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const { buildBrowserActionScript } = require("../src/browserActionScript.mjs");
const {
  desktopDepsAvailable,
  execInBrowserView,
  launchDesktop,
  saveRecording,
  spawnServer,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();

it(
  "allows local browser tools and denies remote access to an already-open view",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`, timeout: 180_000 },
  async () => {
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "omni-navigation-"));
    const recordDir =
      process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
      fs.mkdtempSync(path.join(os.tmpdir(), "omni-navigation-recordings-"));
    const configDir = path.join(scratch, "config");
    fs.mkdirSync(configDir);
    fs.writeFileSync(path.join(configDir, "config.yaml"), "host:\n  host_id: local-preview-host\n");
    const evidence = {};
    const fixture = http.createServer((req, res) => {
      if (req.url === "/redirect") {
        res.writeHead(302, { Location: `http://127.0.0.1:${fixture.address().port}/` }).end();
        return;
      }
      res.setHeader("Content-Type", "text/html");
      res.end(`<h1>Local browser tools</h1><input aria-label="Name"><button>Save</button><p id="saved"></p>
        <script>document.querySelector('button').onclick = () => {
          document.querySelector('#saved').textContent = document.querySelector('input').value;
        };</script>`);
    });
    await new Promise((resolve) => {
      fixture.listen(0, "127.0.0.1", resolve);
    });
    const url = `http://localhost:${fixture.address().port}/`;
    let server;
    let desktop;
    try {
      server = await spawnServer(scratch);
      desktop = await launchDesktop({
        serverUrl: server.serverUrl,
        recordDir,
        env: { OMNIGENT_CONFIG_HOME: configDir },
      });
      const { window } = desktop;
      await window.getByText("What should we build?").waitFor({ timeout: 30_000 });
      const cid = "local-browser-fixture";
      // Fixture host IDs exercise production main/preload authorization, without
      // impersonating a live runner enrollment or granting a policy exception.
      const navigate = (sourceHostId, target = url) =>
        window.evaluate(
          (payload) =>
            window.omnigentDesktop.browserOpenOrNavigate(
              payload.cid,
              payload.target,
              { x: 40, y: 160, width: 900, height: 440 },
              { agent: true, force: true, sourceHostId: payload.sourceHostId },
            ),
          { cid, target, sourceHostId },
        );
      evidence.localNavigation = await navigate("local-preview-host");
      assert.equal(evidence.localNavigation.ok, true, evidence.localNavigation.error);
      await window.evaluate((viewId) => window.omnigentDesktop.browserSetActive(viewId), cid);
      const observe = (script) => execInBrowserView(window, cid, script);
      /* oxlint-disable no-await-in-loop -- Poll navigation and verify sequential source-host requests on one view. */
      const waitForPage = async (expectedHost = "localhost") => {
        for (let attempt = 0; attempt < 100; attempt++) {
          const result = await observe(
            "JSON.stringify([location.hostname, document.querySelector('h1')?.textContent])",
          );
          if (result.result === JSON.stringify([expectedHost, "Local browser tools"])) return;
          await window.waitForTimeout(100);
        }
        throw new Error("Local page never loaded");
      };
      await waitForPage();
      const perform = (sourceHostId, action, params) =>
        window.evaluate(
          (payload) =>
            window.omnigentDesktop.browserPerformAction(
              payload.cid,
              payload.action,
              payload.params,
              payload.sourceHostId,
            ),
          { cid, sourceHostId, action, params },
        );
      evidence.snapshot = await perform("local-preview-host", "snapshot");
      assert.equal(evidence.snapshot.ok, true, evidence.snapshot.error);
      assert.match(JSON.parse(evidence.snapshot.result).tree, /Name/);
      assert.equal(
        (await perform("local-preview-host", "type", { selector: "input", text: "Ada local" })).ok,
        true,
      );
      assert.equal((await perform("local-preview-host", "click", { selector: "button" })).ok, true);
      assert.equal(
        (await observe("document.querySelector('#saved').textContent")).result,
        "Ada local",
      );
      const screenshot = (sourceHostId) =>
        window.evaluate(
          (payload) => window.omnigentDesktop.browserScreenshot(payload.cid, payload.sourceHostId),
          { cid, sourceHostId },
        );
      const localCapture = await screenshot("local-preview-host");
      assert.equal(localCapture.ok, true, localCapture.error);
      assert.match(localCapture.dataUrl, /^data:image\/png;base64,/);
      fs.writeFileSync(
        path.join(recordDir, "local-browser.png"),
        Buffer.from(localCapture.dataUrl.split(",")[1], "base64"),
      );
      evidence.remote = [];
      for (const sourceHostId of ["remote-host", "arca-host", null]) {
        const results = [
          await navigate(sourceHostId, "https://example.com/"),
          await navigate(sourceHostId),
          await screenshot(sourceHostId),
          ...(await Promise.all(
            ["snapshot", "click", "type"].map((action) =>
              perform(sourceHostId, action, { selector: "input", text: "remote" }),
            ),
          )),
          await window.evaluate(
            (payload) =>
              window.omnigentDesktop.browserExecute(payload.cid, payload.js, payload.sourceHostId),
            { cid, sourceHostId, js: buildBrowserActionScript("snapshot") },
          ),
        ];
        for (const result of results) {
          assert.equal(result.ok, false);
          assert.match(result.error, /running on this desktop/);
        }
        evidence.remote.push({ sourceHostId, results });
      }
      assert.equal((await observe("document.querySelector('input').value")).result, "Ada local");
      evidence.redirect = await navigate("local-preview-host", url + "redirect");
      assert.equal(evidence.redirect.ok, true, evidence.redirect.error);
      await waitForPage("127.0.0.1");
      // The redirected loopback spelling must also survive the network guards.
      assert.equal((await observe("location.hostname")).result, "127.0.0.1");
      await window.waitForTimeout(500);
      /* oxlint-enable no-await-in-loop */
    } finally {
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
