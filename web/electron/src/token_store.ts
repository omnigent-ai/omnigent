// Per-origin credential files under ~/.omnigent, encrypted at rest via
// safeStorage. Each sign-in kind gets its own file, NOT the CLI's
// auth_tokens.json: the shapes differ and mixing them would confuse its readers.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/** The part of Electron's `safeStorage` the store uses. */
export interface TokenEncryption {
  isEncryptionAvailable: () => boolean;
  encryptString: (plainText: string) => Buffer;
  decryptString: (encrypted: Buffer) => string;
}

export interface TokenStoreOptions {
  /** File name under `~/.omnigent`. */
  fileName: string;
  /** Names the credentials in the plaintext warning. */
  label: string;
  safeStorage: TokenEncryption;
}

/** Stored credentials are untrusted JSON: `load` returns them as `unknown`. */
export interface TokenStore {
  key: (origin: string) => string;
  save: (origin: string, tokens: unknown) => void;
  load: (origin: string) => unknown;
  remove: (origin: string) => void;
}

function storeKey(origin: string): string {
  return String(origin).replace(/\/+$/, "");
}

export function createTokenStore({ fileName, label, safeStorage }: TokenStoreOptions): TokenStore {
  // Resolved per call so tests (and a changed HOME) see the current home dir.
  const storePath = () => path.join(os.homedir(), ".omnigent", fileName);

  function readStore(): Record<string, unknown> {
    try {
      return JSON.parse(fs.readFileSync(storePath(), "utf8")) as Record<string, unknown>;
    } catch {
      return {};
    }
  }

  function writeStore(store: Record<string, unknown>) {
    const p = storePath();
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, JSON.stringify(store, null, 2), { mode: 0o600 });
    try {
      fs.chmodSync(p, 0o600);
    } catch {
      // Non-POSIX filesystem — the write-time mode is best effort.
    }
  }

  function save(origin: string, tokens: unknown) {
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

  function load(origin: string): unknown {
    const entry = readStore()[storeKey(origin)];
    if (!entry || typeof entry !== "object") return null;
    const { enc, plain } = entry as Record<string, unknown>;
    if (typeof enc === "string") {
      try {
        return JSON.parse(safeStorage.decryptString(Buffer.from(enc, "base64")));
      } catch {
        return null;
      }
    }
    if (plain && typeof plain === "object") return plain;
    return null;
  }

  function remove(origin: string) {
    const key = storeKey(origin);
    const store = readStore();
    if (store[key]) {
      Reflect.deleteProperty(store, key);
      writeStore(store);
    }
  }

  return { key: storeKey, save, load, remove };
}
