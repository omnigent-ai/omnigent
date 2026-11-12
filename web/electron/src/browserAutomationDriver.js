"use strict";

const { createBrowserAutomationTransport } = require("./browserAutomationTransport");

/** Borrow the authorized view; never launch Chrome or close an app-owned tab. */
async function connect(webContents, debuggerClient, { deadlineAt }) {
  const { connect: connectPuppeteer } = await import("puppeteer-core");
  const transport = await createBrowserAutomationTransport(webContents, {
    debuggerClient,
    deadlineAt,
  });
  let browser;
  try {
    browser = await connectPuppeteer({
      transport,
      defaultViewport: null,
      protocolTimeout: Math.max(1, deadlineAt - Date.now()),
    });
    const pages = await browser.pages();
    if (pages.length !== 1) throw new Error("expected exactly one authorized browser page");
    return {
      page: pages[0],
      disconnect() {
        // close() removes protocol listeners synchronously before the next action.
        transport.close();
        void browser.disconnect().catch(() => {});
      },
    };
  } catch (error) {
    transport.close();
    if (browser) void browser.disconnect().catch(() => {});
    throw error;
  }
}

module.exports = { connect };
