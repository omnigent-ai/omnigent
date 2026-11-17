// Writes a Chrome-format macOS profile (Local State + Default/Cookies) with
// v10-encrypted, host-bound values, for the cookie importer's real reader.
"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { deriveMacKey } = require("../../src/chromeCookieStore");

const CHROME_EPOCH_OFFSET_SECONDS = 11644473600;
// What the macOS Keychain would return as "Chrome Safe Storage" for this profile.
const SAFE_STORAGE_PASSWORD = "fixture-safe-storage";

function encryptCookieValue(hostKey, value, key) {
  const bound = Buffer.concat([
    crypto.createHash("sha256").update(hostKey).digest(),
    Buffer.from(value),
  ]);
  const cipher = crypto.createCipheriv("aes-128-cbc", key, Buffer.alloc(16, 0x20));
  return Buffer.concat([Buffer.from("v10", "latin1"), cipher.update(bound), cipher.final()]);
}

/**
 * @param {string} homeDir HOME the desktop app runs with.
 * @param {{ hostKey: string, name: string, value: string }[]} cookies
 * @returns {string} The profile's Cookies database path.
 */
function writeChromeProfile(homeDir, cookies) {
  const root = path.join(homeDir, "Library", "Application Support", "Google", "Chrome");
  const profileDir = path.join(root, "Default");
  fs.mkdirSync(profileDir, { recursive: true });
  fs.writeFileSync(
    path.join(root, "Local State"),
    JSON.stringify({
      profile: { last_used: "Default", info_cache: { Default: { name: "Person 1" } } },
    }),
  );
  const key = deriveMacKey(SAFE_STORAGE_PASSWORD);
  const expiresUtc = Math.round((Date.now() / 1000 + 86_400 + CHROME_EPOCH_OFFSET_SECONDS) * 1e6);
  const rows = cookies.map(
    ({ hostKey, name, value }) =>
      `INSERT INTO cookies (host_key, name, path, encrypted_value, expires_utc, is_secure, is_httponly, samesite)
        VALUES ('${hostKey}', '${name}', '/', X'${encryptCookieValue(hostKey, value, key).toString("hex")}', ${expiresUtc}, 1, 1, -1);`,
  );
  const cookieDbPath = path.join(profileDir, "Cookies");
  const sql = [
    `CREATE TABLE cookies (
      host_key TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL,
      value TEXT NOT NULL DEFAULT '', encrypted_value BLOB NOT NULL DEFAULT X'',
      expires_utc INTEGER NOT NULL DEFAULT 0, has_expires INTEGER NOT NULL DEFAULT 1,
      is_secure INTEGER NOT NULL DEFAULT 0, is_httponly INTEGER NOT NULL DEFAULT 0,
      samesite INTEGER NOT NULL DEFAULT -1, is_persistent INTEGER NOT NULL DEFAULT 1,
      top_frame_site_key TEXT NOT NULL DEFAULT ''
    );`,
    ...rows,
  ].join("\n");
  const result = spawnSync("/usr/bin/sqlite3", [cookieDbPath], { input: sql, encoding: "utf8" });
  if (result.status !== 0) throw new Error(`sqlite3 failed: ${result.stderr}`);
  return cookieDbPath;
}

module.exports = { writeChromeProfile, SAFE_STORAGE_PASSWORD };
