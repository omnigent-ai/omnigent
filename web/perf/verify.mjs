// Verify the actual built client's hydrated fixture content before timing it.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { cdpConnect, ensurePortFree, forEachSequential, waitHttp } from "./cdp.mjs";
import { withLifecycle } from "./lifecycle.mjs";

async function evaluate(cdp, expression, awaitPromise = false) {
  const result = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise });
  if (result.exceptionDetails)
    throw new Error(`fixture verification: ${result.exceptionDetails.text}`);
  return result.result?.value;
}

async function waitFor(check, message, signal, deadline = Date.now() + 30000) {
  signal.throwIfAborted();
  try {
    if (await check()) return;
  } catch (error) {
    if (!/context was destroyed|Cannot find context|Inspected target navigated/i.test(error.message)) throw error;
  }
  if (Date.now() >= deadline) throw new Error(`fixture verification timeout: ${message}`);
  await delay(250, undefined, { signal });
  return waitFor(check, message, signal, deadline);
}

export async function verifyFixtures(base, chrome, port, outputFile) {
  return withLifecycle(async (scope) => {
  const expected = await waitHttp(`${base}/__perf__/fixture-counts`, scope.signal);
  const ids = ["conv_perf_a", "conv_perf_b", "conv_perf_c", "conv_perf_d", "conv_perf_e"];
  for (const id of ids) if (!expected[id]) throw new Error(`missing fixture counts for ${id}`);
  await ensurePortFree(port);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "perf-verify-"));
  scope.directory(profile);
  scope.spawn(
    chrome,
    [
      `--user-data-dir=${profile}`,
      `--remote-debugging-port=${port}`,
      "--headless=new",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-features=Translate,BackForwardCache",
      "--window-size=1600,1000",
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  const report = { base, sessions: {} };
  let cdp;
  scope.cleanup(() => cdp?.close());
    const version = await waitHttp(`http://127.0.0.1:${port}/json/version`, scope.signal);
    cdp = cdpConnect(version.webSocketDebuggerUrl);
    await cdp.ready;
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    cdp.setDefaultSession(sessionId);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await forEachSequential(ids, async (id) => {
      const marker = `SESSMARK-${id.slice(-4).toUpperCase()}`;
      await cdp.send("Page.navigate", { url: `${base}/c/${id}` });
      await waitFor(
        () =>
          evaluate(
            cdp,
            `location.pathname === ${JSON.stringify(`/c/${id}`)} && (document.body?.textContent ?? "").includes(${JSON.stringify(marker)})`,
          ),
        `load ${id}`, scope.signal,
      );
      await evaluate(
        cdp,
        `window.dispatchEvent(new KeyboardEvent("keydown", { key: "f", ctrlKey: true, bubbles: true })); true`,
      );
      await waitFor(
        () => evaluate(cdp, `!document.querySelector('[data-virtualized-transcript]')`),
        `render full window ${id}`, scope.signal,
      );
      await evaluate(
        cdp,
        `[...document.querySelectorAll('.turn-fold-row button[aria-expanded="false"]')].forEach((button) => button.click()); true`,
      );
      await delay(500, undefined, { signal: scope.signal });
      await evaluate(
        cdp,
        `[...document.querySelectorAll('[data-role="assistant"] button[aria-expanded="false"]')].filter((button) => /^Called \\d+ tools?$/.test(button.textContent)).forEach((button) => button.click()); true`,
      );
      const observed = async () =>
        evaluate(
          cdp,
          `(() => {
        const buttons = [...document.querySelectorAll('[data-role="assistant"] button[aria-expanded]')];
        const groups = buttons.filter((button) => /^Called \\d+ tools?$/.test(button.textContent));
        const toolCards = groups.reduce((total, button) => {
          const content = button.parentElement.querySelector(':scope > [data-slot="collapsible-content"]');
          return total + [...(content?.children ?? [])].filter((child) => child.getAttribute("data-slot") === "collapsible" && child.querySelector(':scope > button[title]')).length;
        }, 0);
        return {
          toolCards,
          codeBlocks: document.querySelectorAll('[data-role="assistant"] pre').length,
          reasoningBlocks: buttons.filter((button) => button.textContent.startsWith("Thinking:")).length,
        };
      })()`,
        );
      let rendered;
      try {
        await waitFor(async () => {
          rendered = await observed();
          return Object.keys(expected[id]).every((key) => rendered[key] === expected[id][key]);
        }, `content counts for ${id}`, scope.signal);
      } finally {
        report.sessions[id] = { expected: expected[id], rendered };
        fs.writeFileSync(outputFile, JSON.stringify(report, null, 2));
      }
      process.stdout.write(`verified ${id}: ${JSON.stringify(report.sessions[id])}\n`);
    });
    const workspace = await waitHttp(`${base}/__perf__/workspace-fixture`, scope.signal);
    const expectedTree = workspace.tree.filter((entry) => !entry.path.includes("/") || entry.path.startsWith("dialog_7/") && entry.path.split("/").length === 2).length + 1;
    report.workspace = { expected: { filesRows: expectedTree, viewerLines: workspace.viewerLines, preview: workspace.preview, changesRows: workspace.changes.length }, rendered: {} };
    try {
      await cdp.send("Page.navigate", { url: `${base}/c/conv_perf_d` });
      await waitFor(() => evaluate(cdp, `(document.body?.textContent ?? "").includes("SESSMARK-RF_D")`), "workspace session", scope.signal);
      await evaluate(cdp, `document.querySelector('button[aria-label*="Expand right panel"]')?.click()`);
      await waitFor(() => evaluate(cdp, `!!document.querySelector('[aria-label="Workspace"] [data-index]')`), "files tree", scope.signal);
      await evaluate(cdp, `(() => {
        const rail = document.querySelector('[aria-label="Workspace"]');
        const src = [...rail.querySelectorAll('[data-index] button[aria-expanded]')].find((button) => button.textContent.startsWith('src/'));
        if (src?.getAttribute('aria-expanded') === 'true') src.click();
        const dir = [...rail.querySelectorAll('[data-index] button[aria-expanded]')].find((button) => button.textContent.trim() === 'dialog_7/');
        if (!dir) throw new Error('dialog_7 directory not found');
        if (dir.getAttribute('aria-expanded') !== 'true') dir.click();
      })()`);
      await delay(1500, undefined, { signal: scope.signal });
      async function countVirtualRows(selector) {
        return evaluate(cdp, `(async () => {
          const rail = document.querySelector('[aria-label="Workspace"]');
          const selector = ${JSON.stringify(selector)};
          const first = rail.querySelector(selector);
          if (!first) throw new Error('workspace rows not found: ' + selector);
          let scroller = first.parentElement;
          while (scroller && scroller.scrollHeight <= scroller.clientHeight + 1) scroller = scroller.parentElement;
          if (!scroller) throw new Error('workspace scroller not found');
          const seen = new Set();
          scroller.scrollTop = 0;
          for (let step = 0; step < 500; step++) {
            await new Promise((resolve) => setTimeout(resolve, 100));
            for (const row of rail.querySelectorAll(selector)) seen.add(row.getAttribute('data-index'));
            if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) break;
            scroller.scrollTop += Math.max(100, scroller.clientHeight / 2);
          }
          return seen.size;
        })()`, true);
      }
      report.workspace.rendered.filesRows = await countVirtualRows('div[data-index]');
      if (report.workspace.rendered.filesRows !== expectedTree) throw new Error(`files rows mismatch: ${JSON.stringify(report.workspace)}`);
      const opened = await globalThis.fetch(`${base}/__perf__/openfile`, { method: "POST", body: JSON.stringify({ session: "conv_perf_d", path: workspace.viewer }), signal: scope.signal });
      if (!opened.ok) throw new Error(`file open failed: ${opened.status}`);
      await waitFor(() => evaluate(cdp, `!!document.querySelector('button[aria-label="View mode: Preview"]')`), "viewer", scope.signal);
      await waitFor(async () => {
        report.workspace.rendered.preview = await evaluate(cdp, `(() => {
          const preview = [...document.querySelectorAll('.markdown-preview')].find((element) => element.getBoundingClientRect().width);
          if (!preview) return null;
          return {
            headings: preview.querySelectorAll('h1,h2,h3,h4,h5,h6').length,
            lists: preview.querySelectorAll('ul,ol').length,
            tables: preview.querySelectorAll('table').length,
            codeBlocks: preview.querySelectorAll('pre').length,
          };
        })()`);
        return Object.keys(workspace.preview).every((key) => report.workspace.rendered.preview?.[key] === workspace.preview[key]);
      }, "Markdown preview counts", scope.signal);
      await evaluate(cdp, `(() => {
        const button = [...document.querySelectorAll('button[aria-label="View mode: Preview"]')].find((element) => element.getBoundingClientRect().width);
        button.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }));
      })()`);
      await waitFor(() => evaluate(cdp, `[...document.querySelectorAll('[role="menuitem"]')].some((item) => item.textContent.trim() === 'Edit')`), "viewer mode menu", scope.signal);
      await evaluate(cdp, `[...document.querySelectorAll('[role="menuitem"]')].find((item) => item.textContent.trim() === 'Edit').click()`);
      await waitFor(() => evaluate(cdp, `!!document.querySelector('.monaco-editor .native-edit-context')`), "Monaco viewer", scope.signal);
      await evaluate(cdp, `document.querySelector('.monaco-editor .native-edit-context').focus()`);
      await cdp.send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "End", code: "End", windowsVirtualKeyCode: 35, modifiers: 2 });
      await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "End", code: "End", windowsVirtualKeyCode: 35, modifiers: 2 });
      await waitFor(async () => {
        report.workspace.rendered.viewerLines = await evaluate(cdp, `Math.max(0, ...[...document.querySelectorAll('.monaco-editor .line-numbers')].map((line) => Number(line.textContent.trim()) || 0))`);
        return report.workspace.rendered.viewerLines === workspace.viewerLines;
      }, "viewer lines", scope.signal);
      await evaluate(cdp, `document.querySelector('button[aria-label="Close stepper_00002.md"]')?.click()`);
      await evaluate(cdp, `(() => { const tab = document.querySelector('[aria-label="Workspace"] [data-workspace-tab="changes"]'); tab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 })); tab.click(); })()`);
      await waitFor(() => evaluate(cdp, `document.querySelector('[data-workspace-tab="changes"]')?.getAttribute('aria-selected') === 'true' && !!document.querySelector('[aria-label="Workspace"] li[data-index]')`), "changes list", scope.signal);
      report.workspace.rendered.changesRows = await countVirtualRows('li[data-index]');
      if (report.workspace.rendered.changesRows !== workspace.changes.length) throw new Error(`changes rows mismatch: ${JSON.stringify(report.workspace)}`);
    } finally {
      fs.writeFileSync(outputFile, JSON.stringify(report, null, 2));
    }
    process.stdout.write(`verified workspace: ${JSON.stringify(report.workspace)}\n`);
  });
}
