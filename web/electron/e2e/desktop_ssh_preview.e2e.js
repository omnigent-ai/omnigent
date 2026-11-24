"use strict";

const { after, it } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const { randomUUID } = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { desktopDepsAvailable, launchDesktop, saveRecording } = require("./desktopHarness");
const { eventually, startDesignBackend, startMacCapture } = require("./desktopDesignPromptHarness");
const { openSessionBrowserTab } = require("./desktopBrowserTabHarness");

const deps = desktopDepsAvailable();
const HOST = "c".repeat(32);
const OTHER_HOST = "d".repeat(32);
const trustSources = ["SSH", "settings"];
const journeyResults = {};

after(() => {
  const recordRoot = process.env.OMNIGENT_DESKTOP_RECORD_DIR;
  if (!recordRoot) return;
  fs.mkdirSync(recordRoot, { recursive: true });
  fs.writeFileSync(
    path.join(recordRoot, "evidence.json"),
    JSON.stringify(
      {
        result: trustSources.every((source) => journeyResults[source] === "passed")
          ? "passed"
          : "incomplete",
        journeys: journeyResults,
      },
      null,
      2,
    ),
  );
});

for (const trustSource of trustSources) {
  const settingsTrust = trustSource === "settings";
  it(
    `opens a remote localhost preview through ${trustSource} trust`,
    {
      skip:
        deps.ok && process.platform !== "win32"
          ? false
          : "requires desktop dependencies and POSIX SSH",
      timeout: 180_000,
    },
    async () => {
      const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "omni-ssh-preview-"));
      const recordRoot = path.join(
        process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
          fs.mkdtempSync(path.join(os.tmpdir(), "omni-ssh-preview-recordings-")),
        trustSource.toLowerCase(),
      );
      const sshLog = path.join(scratch, "ssh.jsonl");
      const marker = randomUUID();
      const requests = [];
      const fixture = http.createServer((request, response) => {
        requests.push({ url: request.url, host: request.headers.host });
        response.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" });
        response.end(
          `<title>Remote development preview</title><body style="background:#101928;color:#e5efff;font:20px system-ui;padding:32px"><h1>Remote development preview</h1><p>Loaded through the forwarded port.</p><p id="marker">${marker}</p><button onclick="this.textContent='Interaction works'">Test interaction</button></body>`,
        );
      });
      await new Promise((resolve) => {
        fixture.listen(0, "127.0.0.1", resolve);
      });
      const url = `http://127.0.0.1:${fixture.address().port}/preview`;
      const binDir = path.join(scratch, "bin");
      if (!settingsTrust) {
        fs.mkdirSync(binDir);
        fs.copyFileSync(path.join(__dirname, "fixtures/previewSsh.cjs"), path.join(binDir, "ssh"));
        fs.chmodSync(path.join(binDir, "ssh"), 0o755);
      }
      let backend;
      let desktop;
      let stopCapture = async () => {};
      let recordDir;
      const evidence = { trustSource, marker, url, phases: [] };
      const hold = () =>
        process.env.OMNIGENT_DESKTOP_COMPOSITED_VIDEO === "1"
          ? new Promise((resolve) => {
              setTimeout(resolve, 1800);
            })
          : Promise.resolve();
      const closeDesktop = async () => {
        if (!desktop) return;
        await stopCapture();
        stopCapture = async () => {};
        await desktop.electronApp.close();
        await desktop.stopDisplayCapture();
        saveRecording(recordDir, `${trustSource.toLowerCase()}-preview`);
        desktop = null;
      };
      try {
        backend = await startDesignBackend(scratch);
        const profile = path.join(scratch, "desktop-profile");
        const launch = async (phase) => {
          recordDir = path.join(recordRoot, phase);
          desktop = await launchDesktop({
            serverUrl: backend.serverUrl,
            userDataDir: profile,
            recordDir,
            preload: [path.join(__dirname, "fixtures/previewDialogs.cjs")],
            env: {
              PATH: `${binDir}${path.delimiter}${process.env.PATH}`,
              OMNIGENT_TEST_SSH_LOG: sshLog,
              OMNIGENT_TEST_SSH_SERVER: backend.serverUrl,
              OMNIGENT_TEST_SSH_HOST: HOST,
              OMNIGENT_CONFIG_HOME: path.join(scratch, "desktop-config"),
            },
          });
          const { window, electronApp } = desktop;
          window.setDefaultTimeout(15_000);
          // External host discovery is a fixture; all desktop trust and navigation code is real.
          await window.route(/\/v1\/sessions\/[a-f0-9]+(?:\?.*)?$/, async (route) => {
            if (route.request().method() !== "GET") return route.continue();
            const response = await route.fetch();
            const body = await response.json();
            await route.fulfill({
              response,
              json: { ...body, host_id: body.id === backend.sessionId ? HOST : OTHER_HOST },
            });
          });
          await window.route(/\/v1\/hosts(?:\?.*)?$/, (route) =>
            route.fulfill({
              json: {
                hosts: [
                  {
                    host_id: HOST,
                    name: "preview-host",
                    status: "online",
                    ...(settingsTrust ? { sandbox_provider: "container" } : {}),
                  },
                  { host_id: OTHER_HOST, name: "Unconnected host", status: "online" },
                ],
              },
            }),
          );
          await openSessionBrowserTab(window, backend.serverUrl, backend.sessionId);
          const details = await electronApp.evaluate(({ BrowserWindow, app }) => {
            const win = BrowserWindow.getAllWindows().find((candidate) =>
              candidate.webContents.getURL().startsWith("http"),
            );
            win.setSize(1280, 850);
            win.show();
            win.focus();
            return {
              windowId: win.id,
              pid: process.pid,
              electron: process.versions.electron,
              appPath: app.getAppPath(),
              packaged: app.isPackaged,
              profile: app.getPath("userData"),
            };
          });
          evidence.phases.push({ phase, ...details });
          stopCapture = await startMacCapture(electronApp, details.windowId, recordDir);
          return details;
        };
        const navigate = async (target) => {
          const bar = desktop.window.getByRole("textbox", { name: "Address bar" });
          await bar.fill(target);
          await bar.press("Enter");
        };
        const answer = async (response, details) => {
          const outcome = await eventually(async () => {
            const prompts = await desktop.electronApp.evaluate(() => global.previewPrompts);
            if (prompts.length) return { prompts };
            const error = desktop.window.getByText(/navigation blocked:.*loopback\/internal host/);
            return (await error.isVisible()) ? { error: await error.innerText() } : null;
          }, "SSH preview consent");
          await hold();
          assert.ok(
            outcome.prompts,
            `Expected SSH consent before opening the preview; UI says: ${outcome.error}`,
          );
          assert.equal(requests.length, 0, "no loopback request before consent");
          if (process.env.OMNIGENT_DESKTOP_NATIVE_DIALOGS === "1") {
            const button = response === 1 ? "Allow" : "Not now";
            const clicked = spawnSync(
              "osascript",
              [
                "-l",
                "JavaScript",
                "-e",
                `
          const target = Application("System Events").applicationProcesses.whose({ unixId: ${details.pid} })[0];
          function clickButton(element, depth) {
            if (depth > 15) return false;
            try {
              if (element.role() === "AXButton" && element.name() === "${button}") {
                element.click(); return true;
              }
              return element.uiElements().some(child => clickButton(child, depth + 1));
            } catch { return false; }
          }
          if (!target.windows().some(window => clickButton(window, 0))) throw Error("Preview consent button not found");
        `,
              ],
              { timeout: 10_000, encoding: "utf8" },
            );
            assert.equal(clicked.status, 0, clicked.stderr);
          } else {
            await desktop.electronApp.evaluate(
              (_electron, value) => global.answerPreviewPrompt(value),
              response,
            );
          }
        };
        const page = (target) =>
          desktop.electronApp.evaluate(async (_electron, expected) => {
            const entries = [];
            for (const registry of global.omnigentTestBrowserRegistries)
              registry.forEach((entry) => entries.push(entry));
            const entry = entries.find(
              (candidate) => candidate.view.webContents.getURL() === expected,
            );
            if (!entry || entry.view.webContents.isLoading()) return null;
            return {
              text: await entry.view.webContents.executeJavaScript("document.body.innerText"),
              local: entry.agentContext?.hostIsThisMachine === true,
              host: entry.agentContext?.sourceHostId,
            };
          }, target);
        const agentAction = (sessionId, action, args = {}) =>
          fetch(`${backend.serverUrl}/v1/sessions/${sessionId}/browser/action_request`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action, args }),
            signal: AbortSignal.timeout(30_000),
          }).then((response) => response.json());
        const agentNavigate = (sessionId, target) =>
          agentAction(sessionId, "navigate", { url: target });

        const details = await launch("allow");
        await navigate(url);
        const settingsFile = path.join(details.profile, "settings.json");
        const setTrusted = (entries) => {
          const settings = JSON.parse(fs.readFileSync(settingsFile, "utf8"));
          fs.writeFileSync(
            settingsFile,
            JSON.stringify({ ...settings, trusted_remote_hosts: entries }, null, 2),
          );
        };
        if (settingsTrust) {
          await desktop.window.getByText(/navigation blocked:.*loopback\/internal host/).waitFor();
          const denied = await agentNavigate(backend.sessionId, url);
          assert.equal(denied.ok, false, JSON.stringify(denied));
          assert.match(denied.error, /trusted_remote_hosts/);
          assert.equal(requests.length, 0);
          evidence.denied = denied;
          await hold();
          setTrusted([{ server_url: backend.serverUrl, host_id: HOST }]);
          evidence.settingsSavedAt = new Date().toISOString();
          await navigate(url);
        } else {
          await answer(1, details);
        }
        const loaded = await eventually(async () => {
          const value = await page(url);
          return value?.text.includes(marker) ? value : null;
        }, "forwarded preview marker");
        assert.equal(loaded.local, false, "remote host must not masquerade as this machine");
        assert.equal(loaded.host, HOST);
        evidence.loaded = loaded;
        if (!settingsTrust)
          assert.equal(
            fs.readFileSync(sshLog, "utf8").trim().split("\n").length,
            1,
            "one real CLI subprocess after consent",
          );
        await hold();
        if (!settingsTrust) {
          const subdomainUrl = url.replace("127.0.0.1", "dev.localhost");
          await navigate(subdomainUrl);
          await eventually(
            async () => (await page(subdomainUrl))?.text.includes(marker),
            "subdomain preview",
          );
        }
        const agent = await agentNavigate(backend.sessionId, `${url}?agent`);
        assert.equal(agent.ok, true, JSON.stringify(agent));
        await eventually(
          async () => (await page(`${url}?agent`))?.text.includes(marker),
          "agent preview",
        );
        if (!settingsTrust) {
          const snapshot = await agentAction(backend.sessionId, "snapshot");
          assert.equal(snapshot.ok, true, JSON.stringify(snapshot));
          assert.equal(snapshot.data.url, `${url}?agent`);
          assert.match(snapshot.data.tree, /button "Test interaction"/);
          evidence.snapshot = snapshot.data;
        }
        const clicked = await agentAction(backend.sessionId, "click", { selector: "button" });
        assert.equal(clicked.ok, true, JSON.stringify(clicked));
        await eventually(
          async () => (await page(`${url}?agent`))?.text.includes("Interaction works"),
          "agent interaction",
        );
        if (!settingsTrust) {
          const screenshot = await agentAction(backend.sessionId, "screenshot");
          assert.equal(screenshot.ok, true, JSON.stringify(screenshot));
          assert.match(screenshot.data_url, /^data:image\/png;base64,/);
        }
        assert.equal(
          (await desktop.electronApp.evaluate(() => global.previewPrompts)).length,
          settingsTrust ? 0 : 1,
        );
        await hold();

        if (settingsTrust) {
          setTrusted([]);
          const revoked = await agentAction(backend.sessionId, "snapshot");
          assert.equal(revoked.ok, false, JSON.stringify(revoked));
          evidence.revoked = revoked;
          evidence.result = "passed";
          await hold();
          return;
        }

        await openSessionBrowserTab(desktop.window, backend.serverUrl, backend.otherSessionId);
        const count = requests.length;
        await navigate(`${url}?other-host`);
        await desktop.window.getByText(/navigation blocked:.*loopback\/internal host/).waitFor();
        const denied = await agentNavigate(backend.otherSessionId, `${url}?other-agent`);
        assert.equal(denied.ok, false, JSON.stringify(denied));
        assert.equal(requests.length, count, "another host cannot use the grant");
        await closeDesktop();

        requests.length = 0;
        const restarted = await launch("decline-after-restart");
        await navigate(url);
        await answer(0, restarted);
        await desktop.window.getByText(/navigation blocked:.*loopback\/internal host/).waitFor();
        const declined = await agentNavigate(backend.sessionId, url);
        assert.equal(declined.ok, false, JSON.stringify(declined));
        assert.match(declined.error, /declined/);
        assert.equal(
          (await desktop.electronApp.evaluate(() => global.previewPrompts)).length,
          1,
          "decline must not nag",
        );
        assert.equal(requests.length, 0);
        evidence.declined = declined;
        evidence.result = "passed";
        await hold();
      } catch (error) {
        evidence.error = error.message;
        throw error;
      } finally {
        try {
          await closeDesktop();
        } finally {
          if (backend) await backend.close();
          fixture.closeAllConnections();
          await new Promise((resolve) => {
            fixture.close(resolve);
          });
          fs.mkdirSync(recordRoot, { recursive: true });
          fs.writeFileSync(
            path.join(recordRoot, "evidence.json"),
            JSON.stringify(evidence, null, 2),
          );
          journeyResults[trustSource] = evidence.result || "failed";
          fs.rmSync(scratch, { recursive: true, force: true });
          console.log(`${trustSource} preview evidence: ${recordRoot}`);
        }
      }
    },
  );
}
