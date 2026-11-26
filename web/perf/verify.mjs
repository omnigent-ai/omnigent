// Verify the actual built client's hydrated fixture content before timing it.
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { cdpConnect, ensurePortFree, forEachSequential, waitHttp } from "./cdp.mjs";

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", { expression, returnByValue: true });
  if (result.exceptionDetails)
    throw new Error(`fixture verification: ${result.exceptionDetails.text}`);
  return result.result?.value;
}

async function waitFor(check, message, deadline = Date.now() + 30000) {
  if (await check()) return;
  if (Date.now() >= deadline) throw new Error(`fixture verification timeout: ${message}`);
  await delay(250);
  return waitFor(check, message, deadline);
}

export async function verifyFixtures(base, chrome, port, outputFile) {
  const expected = await waitHttp(`${base}/__perf__/fixture-counts`);
  const ids = ["conv_perf_a", "conv_perf_b", "conv_perf_c", "conv_perf_d", "conv_perf_e"];
  for (const id of ids) if (!expected[id]) throw new Error(`missing fixture counts for ${id}`);
  await ensurePortFree(port);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "perf-verify-"));
  const browser = spawn(
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
  const terminate = () => browser.kill("SIGTERM");
  process.once("SIGTERM", terminate);
  process.once("SIGINT", terminate);
  const report = { base, sessions: {} };
  let cdp;
  try {
    const version = await waitHttp(`http://127.0.0.1:${port}/json/version`);
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
        `load ${id}`,
      );
      await evaluate(
        cdp,
        `window.dispatchEvent(new KeyboardEvent("keydown", { key: "f", ctrlKey: true, bubbles: true })); true`,
      );
      await waitFor(
        () => evaluate(cdp, `!document.querySelector('[data-virtualized-transcript]')`),
        `render full window ${id}`,
      );
      await evaluate(
        cdp,
        `[...document.querySelectorAll('.turn-fold-row button[aria-expanded="false"]')].forEach((button) => button.click()); true`,
      );
      await delay(500);
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
        }, `content counts for ${id}`);
      } finally {
        report.sessions[id] = { expected: expected[id], rendered };
        fs.writeFileSync(outputFile, JSON.stringify(report, null, 2));
      }
      process.stdout.write(`verified ${id}: ${JSON.stringify(report.sessions[id])}\n`);
    });
  } finally {
    process.off("SIGTERM", terminate);
    process.off("SIGINT", terminate);
    cdp?.close();
    browser.kill("SIGTERM");
    if (browser.exitCode === null && browser.signalCode === null) {
      await Promise.race([once(browser, "exit"), delay(5000)]);
      if (browser.exitCode === null && browser.signalCode === null) {
        browser.kill("SIGKILL");
        await once(browser, "exit");
      }
    }
    fs.rmSync(profile, { recursive: true, force: true });
  }
}
