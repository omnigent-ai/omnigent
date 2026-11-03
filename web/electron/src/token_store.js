// Per-origin credential files under ~/.omnigent, encrypted at rest via
// safeStorage. Each sign-in kind gets its own file, NOT the CLI's
// auth_tokens.json: the shapes differ and mixing them would confuse its readers.

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { safeStorage } = require("electron");

function storeKey(origin) {
  return String(origin).replace(/\/+$/, "");
}

/**
 * @param {{ fileName: string, label: string }} options `fileName` under
 *   `~/.omnigent`; `label` names the credentials in the plaintext warning.
 */
function createTokenStore({ fileName, label }) {
  // Resolved per call so tests (and a changed HOME) see the current home dir.
  const storePath = () => path.join(os.homedir(), ".omnigent", fileName);

  function readStore() {
    try {
      return JSON.parse(fs.readFileSync(storePath(), "utf8"));
    } catch {
      return {};
    }
  }

  function writeStore(store) {
    const p = storePath();
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, JSON.stringify(store, null, 2), { mode: 0o600 });
    try {
      fs.chmodSync(p, 0o600);
    } catch {
      // Non-POSIX filesystem — the write-time mode is best effort.
    }
  }

  function save(origin, tokens) {
    const store = readStore();
    if (safeStorage.isEncryptionAvailable()) {
      store[storeKey(origin)] = {
        enc: safeStorage.encryptString(JSON.stringify(tokens)).toString("base64"),
      };
    } else {
      console.warn(`[omnigent] safeStorage unavailable; storing ${label} unencrypted (0600)`);
      store[storeKey(origin)] = { plain: tokens };
    }
    writeStore(store);
  }

  function load(origin) {
    const entry = readStore()[storeKey(origin)];
    if (!entry || typeof entry !== "object") return null;
    if (typeof entry.enc === "string") {
      try {
        return JSON.parse(safeStorage.decryptString(Buffer.from(entry.enc, "base64")));
      } catch {
        return null;
      }
    }
    if (entry.plain && typeof entry.plain === "object") return entry.plain;
    return null;
  }

  function remove(origin) {
    const key = storeKey(origin);
    const store = readStore();
    if (store[key]) {
      Reflect.deleteProperty(store, key);
      writeStore(store);
    }
  }

  return { key: storeKey, save, load, remove };
}

module.exports = { createTokenStore };
