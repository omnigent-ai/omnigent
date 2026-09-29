// Run after building web: node --test --test-concurrency=1 e2e/desktop_design_prompt.e2e.js
// OMNIGENT_PYTHON selects the backend; OMNIGENT_DESKTOP_COMPOSITED_VIDEO=1 films both cases.

"use strict";

const { after, before, describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { desktopDepsAvailable, saveRecording } = require("./desktopHarness");
const {
  MOCK_REPLY,
  eventually,
  startDesignBackend,
  startFormFixture,
  launchDesignDesktop,
  startMacCapture,
} = require("./desktopDesignPromptHarness");
const { startRadixFormFixture } = require("./fixtures/designModalFixture");

const deps = desktopDepsAvailable();
const MODAL = 'dialog[open], [role="dialog"], [role="alertdialog"]';

async function assertEmbeddedFocus(electronApp, ids, input) {
  await eventually(async () => {
    const native = await electronApp.evaluate(({ webContents, BrowserWindow }) => ({
      focusedId: webContents.getFocusedWebContents()?.id,
      focusedWindow: BrowserWindow.getFocusedWindow()?.id,
    }));
    const renderer = await input.evaluate((element) => ({
      activeInput: document.activeElement === element,
      documentFocused: document.hasFocus(),
      activeElement: document.activeElement?.id,
    }));
    const diagnostic = JSON.stringify({ expected: ids, ...native, ...renderer });
    assert.notEqual(ids.viewId, ids.shellId);
    assert.equal(native.focusedId, ids.viewId, diagnostic);
    assert.equal(native.focusedWindow, ids.windowId, diagnostic);
    assert.ok(renderer.activeInput && renderer.documentFocused, diagnostic);
    return true;
  }, "native embedded WebContents focus and active instruction input");
}

async function assertAnchored(page) {
  return eventually(async () => {
    const geometry = await page.evaluate(() => {
      const rect = (selector) => document.querySelector(selector).getBoundingClientRect().toJSON();
      return {
        field: rect("#scenario-period"),
        popup: rect("#__omni-popup"),
        dialog: rect("#capacity-dialog"),
        viewport: { width: innerWidth, height: innerHeight },
        clickable: ["input", "send", "close"].every((name) => {
          const element = document.getElementById(`__omni-popup-${name}`);
          const box = element.getBoundingClientRect();
          return element.contains(
            document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2),
          );
        }),
      };
    });
    const { field, popup, viewport } = geometry;
    const expectedLeft = Math.max(
      8,
      Math.min(field.x + field.width / 2 - popup.width / 2, viewport.width - popup.width - 8),
    );
    const gap = Math.min(
      Math.abs(popup.top - field.bottom - 8),
      Math.abs(field.top - popup.bottom - 8),
    );
    assert.ok(popup.width > 0 && popup.height > 0, "floating editor is visible");
    assert.ok(Math.abs(popup.left - expectedLeft) < 2 && gap < 2, JSON.stringify(geometry));
    assert.ok(
      popup.left >= 0 &&
        popup.right <= viewport.width &&
        popup.top >= 0 &&
        popup.bottom <= viewport.height,
      JSON.stringify(geometry),
    );
    assert.ok(geometry.clickable, "input, Send and close must be hit-testable above the modal");
    return geometry;
  }, "visible, clickable popup anchored next to Period");
}

async function assertModalIntact(page, kind) {
  const state = await page.locator("#capacity-dialog").evaluate(
    (dialog, variant) => ({
      open:
        variant === "native"
          ? dialog.matches("dialog[open]:modal")
          : dialog.getAttribute("data-state") === "open",
      period: document.getElementById("scenario-period").value,
      submits: window.designNativeSubmits,
    }),
    kind,
  );
  assert.deepEqual(state, { open: true, period: "W41", submits: 0 });
}

async function editPeriod(page) {
  const period = page.locator("#scenario-period");
  await period.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.type("W42");
  assert.equal(await period.inputValue(), "W42");
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.type("W41");
  assert.equal(await period.inputValue(), "W41");
}

describe(
  "desktop in-page floating design editor",
  {
    concurrency: 1,
    skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`,
  },
  () => {
    let backend;
    let electronApp;
    let window;
    let nativeWindow;
    let windowIds;
    let recordDir;
    const fixtures = {};
    const messages = [];

    before(
      async () => {
        const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-design-popup-"));
        recordDir = process.env.OMNIGENT_DESKTOP_RECORD_DIR || path.join(tmpDir, "recordings");
        fs.mkdirSync(recordDir, { recursive: true });
        fixtures.native = await startFormFixture();
        fixtures.radix = await startRadixFormFixture();
        backend = await startDesignBackend(tmpDir);
        ({ electronApp, window } = await launchDesignDesktop(tmpDir, recordDir, backend.serverUrl));
        nativeWindow = await electronApp.browserWindow(window);
        windowIds = await nativeWindow.evaluate((win) => {
          win.setSize(1400, 900);
          win.show();
          win.focus();
          return { windowId: win.id, shellId: win.webContents.id };
        });
        // macOS may show the test window without making its application foreground.
        await electronApp.evaluate(({ app }) => {
          if (process.platform === "darwin") app.focus({ steal: true });
        });
        await eventually(
          () => nativeWindow.evaluate((win) => win.isFocused()),
          "foreground test window",
        );
        window.on("request", (request) => {
          if (
            request.method() === "POST" &&
            request.url() === `${backend.serverUrl}/v1/sessions/${backend.sessionId}/events`
          ) {
            const body = request.postDataJSON();
            if (body?.type === "message") messages.push(body);
          }
        });
        await window.goto(`${backend.serverUrl}/c/${backend.sessionId}`);
        await window.getByRole("button", { name: /^(Expand|Collapse) right panel$/ }).waitFor();
        const expand = window.getByRole("button", { name: "Expand right panel", exact: true });
        if (await expand.isVisible()) await expand.click();
        const sidebar = window.getByRole("button", { name: "Close sidebar", exact: true });
        if (await sidebar.isVisible()) await sidebar.click();
        await window.getByRole("tab", { name: "Browser", exact: true }).click();
        if (process.env.OMNIGENT_DESKTOP_COMPOSITED_VIDEO === "1") {
          const resize = window.getByRole("separator", { name: "Resize panel", exact: true });
          for (let step = 0; step < 10; step += 1) {
            // oxlint-disable-next-line no-await-in-loop -- Resize using ordered keyboard gestures.
            await resize.press("ArrowLeft");
          }
        }
      },
      { timeout: 180_000 },
    );

    after(async () => {
      try {
        await electronApp?.close();
      } finally {
        await Promise.all([fixtures.native?.close(), fixtures.radix?.close(), backend?.close()]);
        if (recordDir) {
          saveRecording(recordDir, "design-popup-renderer");
          console.log(`Desktop design popup artifacts: ${recordDir}`);
        }
      }
    });

    for (const kind of ["native", "radix"]) {
      it(
        `${kind} dialog keeps typing, cancellation and chat submission inside the floating editor`,
        { timeout: 180_000 },
        async (t) => {
          const fixture = fixtures[kind];
          const exitDesign = window.getByRole("button", { name: "Exit design mode", exact: true });
          if (await exitDesign.isVisible()) await exitDesign.click();
          const address = window.getByRole("textbox", { name: "Address bar", exact: true });
          await address.click();
          await window.keyboard.press("ControlOrMeta+A");
          await window.keyboard.type(fixture.url);
          await window.keyboard.press("Enter");
          const page = await eventually(
            () =>
              electronApp
                .context()
                .pages()
                .find((candidate) => candidate.url() === fixture.url),
            "embedded fixture WebContentsView",
          );
          const period = page.locator("#scenario-period");
          const popup = page.locator("#__omni-popup");
          const input = page.locator("#__omni-popup-input");
          await period.waitFor();
          await page.evaluate(() => {
            window.designNativeSubmits = 0;
            document.addEventListener(
              "submit",
              () => {
                window.designNativeSubmits += 1;
              },
              true,
            );
          });
          const viewId = await electronApp.evaluate(
            ({ BrowserWindow }, { windowId, url }) =>
              BrowserWindow.fromId(windowId).contentView.children.find(
                (view) => view.webContents?.getURL() === url,
              )?.webContents.id,
            { windowId: windowIds.windowId, url: fixture.url },
          );
          assert.ok(viewId, "fixture must be hosted in a native WebContentsView");
          const ids = { ...windowIds, viewId };
          const captureDir = path.join(recordDir, kind);
          fs.mkdirSync(captureDir, { recursive: true });
          t.after(await startMacCapture(electronApp, ids.windowId, captureDir));
          t.after(async () => {
            const diagnostic = await page
              .evaluate(() => ({
                focusedElement: document.activeElement?.id,
                submits: window.designNativeSubmits,
                events: window["__reproEvents"],
              }))
              .catch((error) => ({ error: error.message }));
            fs.writeFileSync(
              path.join(recordDir, `${kind}-form-diagnostics.json`),
              JSON.stringify(diagnostic, null, 2),
            );
          });

          // CDP clicks do not foreground a macOS app like a user's OS-level click.
          await electronApp.evaluate(({ app, BrowserWindow }, id) => {
            if (process.platform === "darwin") app.focus({ steal: true });
            BrowserWindow.fromId(id).focus();
          }, ids.windowId);
          await window.getByRole("button", { name: "Enter design mode", exact: true }).waitFor();
          await editPeriod(page);
          await assertEmbeddedFocus(electronApp, ids, period);
          await assertModalIntact(page, kind);
          const beforeMessages = messages.length;
          await window.getByRole("button", { name: "Enter design mode", exact: true }).click();
          await page.locator("#__omni-design-layer").waitFor({ state: "attached" });

          const selectPeriod = async () => {
            await period.click();
            await input.waitFor();
            const mounted = await period.evaluate((field, selector) => {
              const layer = document.getElementById("__omni-design-layer");
              const modal = field.closest(selector);
              return {
                insideDialog:
                  layer.parentElement === modal &&
                  document.getElementById("__omni-popup").closest(selector) === modal,
                manualPopover:
                  layer.getAttribute("popover") === "manual" && layer.matches(":popover-open"),
              };
            }, MODAL);
            assert.deepEqual(mounted, { insideDialog: true, manualPopover: true });
            assert.equal(
              await window
                .getByRole("region", {
                  name: "Design instruction",
                  exact: true,
                  includeHidden: true,
                })
                .count(),
              0,
              "editor must not move into a shell toolbar",
            );
            assert.equal(await input.inputValue(), "", "selection starts a fresh instruction");
            await assertEmbeddedFocus(electronApp, ids, input);
            return assertAnchored(page);
          };

          await selectPeriod();
          await page.keyboard.type("asdf", { delay: 80 });
          assert.equal(await input.inputValue(), "asdf");
          await assertEmbeddedFocus(electronApp, ids, input);
          assert.ok(
            await page.evaluate(() =>
              window["__reproEvents"].some(
                (event) =>
                  event.type === "keydown" &&
                  event.target === "__omni-popup-input" &&
                  event.key === "a" &&
                  event.trusted,
              ),
            ),
            "trusted keyboard input must reach the floating editor",
          );
          await assertModalIntact(page, kind);
          await page.keyboard.press("Shift+Enter");
          await assertModalIntact(page, kind);
          assert.equal(messages.length, beforeMessages, "Shift+Enter must not submit either form");
          if (process.env.OMNIGENT_DESKTOP_COMPOSITED_VIDEO === "1") {
            await page.waitForTimeout(1200);
          }
          await page.keyboard.press("Escape");
          await popup.waitFor({ state: "hidden" });
          await assertModalIntact(page, kind);
          assert.equal(messages.length, beforeMessages, "Escape must not post chat");

          await selectPeriod();
          await page.keyboard.type("Cancel this change.");
          await page.locator("#__omni-popup-close").click();
          await popup.waitFor({ state: "hidden" });
          await assertModalIntact(page, kind);
          assert.equal(
            messages.length,
            beforeMessages,
            "close must not post chat or submit the form",
          );

          let geometry = await selectPeriod();
          let instruction = "Use a week picker for Period.";
          await page.keyboard.type(instruction);
          if (kind === "radix") {
            const form = page.locator("#capacity-dialog");
            const layout = await form.evaluate((element) => ({
              tag: element.tagName,
              transform: getComputedStyle(element).transform,
              overflow: getComputedStyle(element).overflowY,
              scrollTop: element.scrollTop,
            }));
            assert.equal(layout.tag, "FORM", "popup must also live inside a native form");
            assert.notEqual(layout.transform, "none");
            assert.equal(layout.overflow, "auto");
            assert.ok(
              geometry.popup.left < geometry.dialog.left ||
                geometry.popup.right > geometry.dialog.right,
              "fixture must exercise escaping the dialog's clipping bounds",
            );
            await page.mouse.move(
              geometry.field.x + geometry.field.width / 2,
              geometry.field.y + geometry.field.height / 2,
            );
            await page.mouse.wheel(0, 64);
            await eventually(
              async () =>
                (await form.evaluate((element) => element.scrollTop)) > layout.scrollTop + 20,
              "native wheel scroll inside Radix form",
            );
            const scrolled = await assertAnchored(page);
            assert.ok(
              Math.abs(scrolled.field.y - geometry.field.y) > 20,
              "scroll must move the selected field",
            );

            await nativeWindow.evaluate((win) => win.setSize(1360, 800));
            await eventually(
              async () => (await page.evaluate(() => innerHeight)) !== scrolled.viewport.height,
              "resized embedded viewport",
            );
            geometry = await assertAnchored(page);
            await electronApp.evaluate(
              ({ webContents }, id) => webContents.fromId(id).setZoomFactor(1.1),
              viewId,
            );
            await eventually(
              async () => (await page.evaluate(() => innerWidth)) < geometry.viewport.width,
              "zoomed embedded viewport",
            );
            await assertAnchored(page);
            await assertEmbeddedFocus(electronApp, ids, input);
            assert.equal(await input.inputValue(), instruction, "re-anchoring preserves the draft");
            await input.click();
            instruction += " Keep W41 as the default.";
            await page.keyboard.press("ControlOrMeta+A");
            await page.keyboard.type(instruction);
          }

          assert.equal(await input.inputValue(), instruction);
          await assertModalIntact(page, kind);
          const replies = window.getByText(MOCK_REPLY, { exact: true });
          const beforeReplies = await replies.count();
          const [response] = await Promise.all([
            window.waitForResponse(
              (result) =>
                result.request().method() === "POST" &&
                result.url() === `${backend.serverUrl}/v1/sessions/${backend.sessionId}/events` &&
                result.request().postDataJSON()?.type === "message",
              { timeout: 45_000 },
            ),
            kind === "native"
              ? page.locator("#__omni-popup-send").click()
              : page.keyboard.press("Enter"),
          ]);
          assert.equal(
            response.status(),
            202,
            "normal chat event endpoint accepts design instructions",
          );
          assert.equal(messages.length, beforeMessages + 1, "exactly one chat POST per submission");
          const sent = JSON.stringify(messages[beforeMessages]);
          assert.match(sent, /Design Mode/);
          assert.ok(sent.includes("#scenario-period"));
          assert.ok(sent.includes(instruction));
          await eventually(
            async () => (await replies.count()) === beforeReplies + 1,
            "mock agent reply in the real chat",
            45_000,
          );
          await popup.waitFor({ state: "hidden" });
          await assertModalIntact(page, kind);

          await selectPeriod();
          await page.keyboard.type("Discard on exit.");
          await window.getByRole("button", { name: "Exit design mode", exact: true }).click();
          await page.locator("#__omni-design-layer").waitFor({ state: "detached" });
          await editPeriod(page);
          await assertEmbeddedFocus(electronApp, ids, period);
          await assertModalIntact(page, kind);
          assert.equal(messages.length, beforeMessages + 1, "disabling must discard the draft");
          if (kind === "radix") {
            assert.equal(await page.locator("#scenario-values").textContent(), "Period: W41");
            // Positive control: ordinary Enter still reaches the fixture's native form handler.
            await page.keyboard.press("Enter");
            await eventually(
              () => page.evaluate(() => window.designNativeSubmits === 1),
              "ordinary form submission after disabling design mode",
            );
          }
        },
      );
    }
  },
);
