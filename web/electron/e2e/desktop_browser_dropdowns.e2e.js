// Agent browser actions against common dropdown widgets; see e2e/README.md to run.

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  desktopDepsAvailable,
  launchDesktop,
  openBrowserFixture,
  saveRecording,
  spawnServer,
} = require("./desktopHarness");
const { eventually } = require("./desktopDesignPromptHarness");
const { startDropdownFixture } = require("./fixtures/dropdownFixture");

const deps = desktopDepsAvailable();
const CONVERSATION_ID = "dropdown-actions";

function refFor(tree, role, name) {
  const escaped = JSON.stringify(name).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = tree.match(new RegExp(`- ${role} ${escaped} \\[ref=(\\d+)\\]`));
  return match ? Number(match[1]) : null;
}

describe(
  "desktop shell — agent browser actions on dropdowns",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;
    let fixture;
    let desktop;
    let page;
    let recordDir;

    const perform = async (action, params = {}) => {
      const result = await desktop.window.evaluate(
        ({ cid, action: requested, params: requestedParams }) =>
          window.omnigentDesktop.browserPerformAction(cid, requested, requestedParams),
        { cid: CONVERSATION_ID, action, params },
      );
      assert.equal(result.ok, true, `${action} failed: ${result.error}`);
      return result.result;
    };
    const snapshot = async () => JSON.parse(await perform("snapshot")).tree;
    const fieldState = () => page.locator("#dropdown-state").innerText();
    const pageEvents = () => page.evaluate(() => window["__dropdownEvents"].splice(0));

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-dropdowns-"));
      recordDir = process.env.OMNIGENT_DESKTOP_RECORD_DIR || path.join(tmpDir, "recordings");
      server = await spawnServer(tmpDir);
      fixture = await startDropdownFixture();
      desktop = await launchDesktop({
        serverUrl: server.serverUrl,
        recordDir,
        userDataDir: path.join(tmpDir, "profile"),
      });
      const { electronApp, window } = desktop;
      await window.getByText("What should we build?").waitFor({ timeout: 30_000 });
      await window.waitForFunction(() => !!window.omnigentDesktop?.browserPerformAction);
      const opened = await openBrowserFixture(window, CONVERSATION_ID, fixture.url);
      assert.equal(opened.ok, true, opened.error);
      const active = await window.evaluate(
        (cid) => window.omnigentDesktop.browserSetActive(cid),
        CONVERSATION_ID,
      );
      assert.equal(active.ok, true, active.error);
      page = await eventually(
        () =>
          electronApp
            .context()
            .pages()
            .find((candidate) => candidate.url() === fixture.url),
        "dropdown fixture view",
      );
      await page.locator("#dropdown-state").waitFor();
      await eventually(async () => (await snapshot()).includes("Deploy stage"), "laid-out fixture");
    });

    after(async () => {
      try {
        await desktop?.electronApp.close().catch(() => {});
        await desktop?.stopDisplayCapture().catch(() => {});
        if (recordDir) saveRecording(recordDir, "browser-dropdown-actions");
      } finally {
        await Promise.all([fixture?.close(), server?.close()]);
        if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
      }
    });

    it("opens a mousedown dropdown and selects an option by ref", async () => {
      const tree = await snapshot();
      const region = refFor(tree, "combobox", "Region");
      assert.ok(region, `Region combobox missing from snapshot:\n${tree}`);
      await pageEvents();
      await perform("click", { ref: region });
      const opened = await snapshot();
      const events = await pageEvents();
      const option = refFor(opened, "option", "us-west-2");
      assert.ok(
        option,
        `Region options missing after browser click; state="${await fieldState()}", ` +
          `events=${JSON.stringify(events)}\n${opened}`,
      );
      await perform("click", { ref: option });
      await eventually(async () => /Region: us-west-2/.test(await fieldState()), "Region value");
    });

    it("lists native select options in the snapshot and chooses one by ref", async () => {
      const tree = await snapshot();
      assert.ok(refFor(tree, "combobox", "Account (native select)"), `Account missing:\n${tree}`);
      for (const name of ["dev", "staging", "prod"]) {
        assert.ok(refFor(tree, "option", name), `option "${name}" missing from snapshot:\n${tree}`);
      }
      const staging = refFor(tree, "option", "staging");
      assert.ok(staging, `staging option missing:\n${tree}`);
      await perform("click", { ref: staging });
      await eventually(async () => /Account: staging/.test(await fieldState()), "Account value");
      assert.match(await snapshot(), /- option "staging" \[ref=\d+\] \[selected\]/);
    });

    it("opens a Radix select and selects an option by ref", async () => {
      const tree = await snapshot();
      const environment = refFor(tree, "combobox", "Environment");
      assert.ok(environment, `Environment combobox missing from snapshot:\n${tree}`);
      await perform("click", { ref: environment });
      const opened = await eventually(async () => {
        const next = await snapshot();
        return refFor(next, "option", "canary") ? next : null;
      }, "Radix options in snapshot");
      await perform("click", { ref: refFor(opened, "option", "canary") });
      await eventually(async () => /Environment: canary/.test(await fieldState()), "Radix value");
    });

    it("returns from a click whose press navigates away", async () => {
      // A silent message channel delays the release until after the navigation commits.
      await page.evaluate(() => {
        window.MessageChannel = class {
          port1 = { close() {} };
          port2 = { postMessage() {} };
        };
        const leave = document.createElement("div");
        leave.id = "leave";
        leave.textContent = "Leave";
        leave.addEventListener("mousedown", () => location.assign("/left"));
        document.body.append(leave);
      });
      const result = await Promise.race([
        perform("click", { selector: "#leave" }),
        new Promise((resolve) => {
          setTimeout(resolve, 10_000, "still pending after 10s");
        }),
      ]);
      assert.equal(result, "ok");
      await eventually(() => page.url().endsWith("/left"), "navigated page");
    });
  },
);
