// Enables the macOS-only Chrome cookie import on any platform for desktop e2e
// runs, preloaded before main.js. Keychain and the native confirmation are faked.
"use strict";

const path = require("node:path");
const { ipcMain } = require("electron");

const SRC = path.join(__dirname, "..", "..", "src");
const { SAFE_STORAGE_PASSWORD } = require("./chromeProfileFixture");

globalThis.cookieImportPrompts = [];

const handle = ipcMain.handle.bind(ipcMain);
ipcMain.handle = (channel, listener) =>
  handle(
    channel,
    channel === "omnigent:get-desktop-features"
      ? async (...args) => {
          const features = await listener(...args);
          return features && { ...features, chromeCookieImport: true };
        }
      : listener,
  );

const store = require(path.join(SRC, "chromeCookieStore.js"));
const browserIpc = require(path.join(SRC, "browserIpc.js"));
const registerBrowserIpc = browserIpc.registerBrowserIpc;
browserIpc.registerBrowserIpc = (options) =>
  registerBrowserIpc({
    ...options,
    platform: "darwin",
    chromeCookieStore: {
      ...store,
      importSiteCookies: (args) =>
        store.importSiteCookies({ ...args, readPassword: async () => SAFE_STORAGE_PASSWORD }),
    },
    confirmCookieImport: async ({ parentWindow: _parentWindow, ...prompt }) => {
      globalThis.cookieImportPrompts.push(prompt);
      return true;
    },
  });

const notice = require(path.join(SRC, "browserSecurityKeyNotice.js"));
const attachSecurityKeyNotice = notice.attachSecurityKeyNotice;
notice.attachSecurityKeyNotice = (contents, options) =>
  attachSecurityKeyNotice(contents, { ...options, cookieImport: true });
