// Unit tests for the encrypted per-origin credential store (src/token_store.ts),
// run with `node --test`. safeStorage is a reversible stub; HOME is a temp dir.

import { describe, it, beforeEach, afterEach, mock } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { createTokenStore } from "../src/token_store.ts";

const safeStorage = {
  available: true,
  isEncryptionAvailable: () => safeStorage.available,
  encryptString: (text: string) => Buffer.from(`enc:${text}`),
  decryptString: (buffer: Buffer) => {
    const text = buffer.toString();
    if (!text.startsWith("enc:")) throw new Error("bad ciphertext");
    return text.slice(4);
  },
};

let home: string;
let warn: ReturnType<typeof mock.method>;
beforeEach(() => {
  home = fs.mkdtempSync(path.join(os.tmpdir(), "omni-token-store-"));
  mock.method(os, "homedir", () => home);
  warn = mock.method(console, "warn", () => {});
  safeStorage.available = true;
});
afterEach(() => {
  mock.restoreAll();
  fs.rmSync(home, { recursive: true, force: true });
});

const file = () => path.join(home, ".omnigent", "test_tokens.json");
const store = () =>
  createTokenStore({ fileName: "test_tokens.json", label: "test tokens", safeStorage });

describe("createTokenStore", () => {
  it("encrypts at rest, keyed by the slash-stripped origin", () => {
    store().save("https://a.test/", { refresh_token: "r" });
    const raw = JSON.parse(fs.readFileSync(file(), "utf8")) as Record<string, { enc: unknown }>;
    assert.deepEqual(Object.keys(raw), ["https://a.test"]);
    assert.equal(typeof raw["https://a.test"]?.enc, "string");
    assert.doesNotMatch(fs.readFileSync(file(), "utf8"), /"r"/);
    assert.deepEqual(store().load("https://a.test"), { refresh_token: "r" });
    assert.equal(fs.statSync(file()).mode & 0o777, 0o600);
  });

  it("reads entries written by earlier builds, encrypted or plain", () => {
    fs.mkdirSync(path.dirname(file()), { recursive: true });
    fs.writeFileSync(
      file(),
      JSON.stringify({
        "https://a.test": { enc: Buffer.from('enc:{"x":1}').toString("base64") },
        "https://b.test": { plain: { y: 2 } },
      }),
    );
    assert.deepEqual(store().load("https://a.test"), { x: 1 });
    assert.deepEqual(store().load("https://b.test"), { y: 2 });
  });

  it("falls back to plaintext without a keychain, and warns", () => {
    safeStorage.available = false;
    store().save("https://a.test", { r: 1 });
    assert.deepEqual(JSON.parse(fs.readFileSync(file(), "utf8"))["https://a.test"], {
      plain: { r: 1 },
    });
    assert.match(String(warn.mock.calls[0]?.arguments[0]), /storing test tokens unencrypted/);
  });

  it("returns null for undecryptable, corrupt, or missing entries", () => {
    fs.mkdirSync(path.dirname(file()), { recursive: true });
    fs.writeFileSync(
      file(),
      JSON.stringify({ "https://a.test": { enc: Buffer.from("junk").toString("base64") } }),
    );
    assert.equal(store().load("https://a.test"), null);
    assert.equal(store().load("https://missing.test"), null);
    fs.writeFileSync(file(), "{ not json");
    assert.equal(store().load("https://a.test"), null);
  });

  it("removes one origin and leaves the others", () => {
    store().save("https://a.test", { a: 1 });
    store().save("https://b.test", { b: 1 });
    store().remove("https://a.test/");
    assert.equal(store().load("https://a.test"), null);
    assert.deepEqual(store().load("https://b.test"), { b: 1 });
  });

  it("exposes the key it stores under", () => {
    assert.equal(store().key("https://a.test///"), "https://a.test");
  });
});
