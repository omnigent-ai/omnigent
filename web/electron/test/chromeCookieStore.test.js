// Tests for src/chromeCookieStore.js, run with `node --test`. Anything that
// shells out to `sqlite3` is skipped where that CLI is missing or predates `-json`
// (sqlite 3.33); the Keychain and the Electron session are always injected.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const store = require("../src/chromeCookieStore");

// Probe the same binary and output mode the module uses, not whatever `PATH` resolves.
const hasSqlite3 = (() => {
  try {
    execFileSync("/usr/bin/sqlite3", ["-json", ":memory:", "SELECT 1"], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
})();

/** Unix seconds → Chrome's microseconds-since-1601. */
function chromeExpiresUtc(unixSeconds) {
  return Math.round((unixSeconds + 11644473600) * 1e6);
}

/** AES-128-CBC in Chrome's `v10` envelope (IV = 16 spaces). */
function encryptChromeValue(plaintext, key, prefix = "v10") {
  const cipher = crypto.createCipheriv("aes-128-cbc", key, Buffer.alloc(16, 0x20));
  return Buffer.concat([Buffer.from(prefix, "latin1"), cipher.update(plaintext), cipher.final()]);
}

/** Chrome 130+ binds a value to its host via a SHA-256 prefix. */
function boundPlaintext(hostKey, value) {
  return Buffer.concat([crypto.createHash("sha256").update(hostKey).digest(), Buffer.from(value)]);
}

describe("chromeCookieStore — scope trees", () => {
  it("normalizes URL hostnames and rejects anything that isn't one", () => {
    assert.equal(store.normalizeHost("Mail.Google.com"), "mail.google.com");
    assert.equal(store.normalizeHost("example.com."), "example.com");
    assert.equal(store.normalizeHost("[::1]"), "::1");
    for (const bad of ["", "   ", "exa'mple.com", "a b.com", "example.com; DROP TABLE cookies"]) {
      assert.throws(() => store.normalizeHost(bad), /unsupported host/, bad);
    }
  });

  it("covers the scope itself, its subdomains, and the domain cookies it inherits", () => {
    assert.equal(store.cookieAppliesToScope("accounts.google.com", "google.com"), true);
    assert.equal(store.cookieAppliesToScope(".accounts.google.com", "google.com"), true);
    assert.equal(store.cookieAppliesToScope("google.com", "google.com"), true);
    // Cookies never travel up: a child scope can't claim a sibling or the parent.
    assert.equal(store.cookieAppliesToScope("google.com", "accounts.google.com"), false);
    assert.equal(store.cookieAppliesToScope(".google.com", "accounts.google.com"), false);
    assert.equal(store.cookieAppliesToScope("mail.google.com", "accounts.google.com"), false);
    // Suffix must be on a label boundary, not a string suffix.
    assert.equal(store.cookieAppliesToScope("notgoogle.com", "google.com"), false);
    assert.equal(store.cookieAppliesToScope("", "google.com"), false);
    // Inherited cookies must be domain cookies; a host-only parent never reaches.
    assert.equal(store.cookieInScope(".google.com", "mail.google.com"), true);
    assert.equal(store.cookieInScope("google.com", "mail.google.com"), false);
    assert.equal(store.cookieInScope("mail.google.com", "mail.google.com"), true);
  });

  it("keeps session cookies and drops expired/foreign ones", () => {
    const now = Date.UTC(2026, 0, 1);
    const rows = [
      { hostKey: ".example.com", name: "session", expiresUtc: 0 },
      { hostKey: ".example.com", name: "future", expiresUtc: chromeExpiresUtc(now / 1000 + 3600) },
      { hostKey: ".example.com", name: "past", expiresUtc: chromeExpiresUtc(now / 1000 - 60) },
      { hostKey: ".other.com", name: "foreign", expiresUtc: 0 },
    ];
    const kept = store.selectCookiesForScope(rows, "app.example.com", now);
    assert.deepEqual(
      kept.map((row) => row.name),
      ["session", "future"],
    );
  });
});

describe("chromeCookieStore — decryption", () => {
  const key = store.deriveMacKey("test-password");

  it("derives Chrome's 16-byte PBKDF2-SHA1 key", () => {
    assert.equal(key.length, 16);
    assert.deepEqual(key, crypto.pbkdf2Sync("test-password", "saltysalt", 1003, 16, "sha1"));
    assert.notDeepEqual(key, store.deriveMacKey("other-password"));
  });

  it("round-trips a v10 value", () => {
    const encrypted = encryptChromeValue(Buffer.from("secret-token"), key);
    assert.equal(store.decryptChromeValue(encrypted, key).toString("utf8"), "secret-token");
  });

  it("round-trips a v11 value", () => {
    const encrypted = encryptChromeValue(Buffer.from("secret-token"), key, "v11");
    assert.equal(store.decryptChromeValue(encrypted, key).toString("utf8"), "secret-token");
  });

  it("rejects unknown envelopes instead of guessing", () => {
    assert.throws(
      () => store.decryptChromeValue(Buffer.from("v20\x01\x02", "latin1"), key),
      /unsupported cookie encryption \(v20\)/,
    );
    assert.throws(() => store.decryptChromeValue(Buffer.alloc(0), key), /empty encrypted value/);
  });

  it("strips the SHA-256(host_key) domain binding when present", () => {
    const encrypted = encryptChromeValue(boundPlaintext(".example.com", "abc123"), key);
    const value = store.decodeCookieValue(
      { hostKey: ".example.com", value: "", encryptedValue: encrypted },
      key,
    );
    assert.equal(value, "abc123");
  });

  it("keeps a plaintext that only looks like it has a hash prefix", () => {
    const plaintext = Buffer.concat([crypto.randomBytes(32), Buffer.from("real-value")]);
    const encrypted = encryptChromeValue(plaintext, key);
    const value = store.decodeCookieValue(
      { hostKey: ".example.com", value: "", encryptedValue: encrypted },
      key,
    );
    assert.equal(value, plaintext.toString("utf8"));
  });

  it("returns an empty value when the hash is the whole plaintext", () => {
    // An empty-valued cookie in a hash-prefixed profile decrypts to 32 bytes.
    const encrypted = encryptChromeValue(boundPlaintext(".example.com", ""), key);
    const value = store.decodeCookieValue(
      { hostKey: ".example.com", value: "", encryptedValue: encrypted },
      key,
    );
    assert.equal(value, "");
  });

  it("keeps a 32-byte value that isn't the host binding", () => {
    const plaintext = Buffer.from("0123456789abcdef0123456789abcdef");
    const encrypted = encryptChromeValue(plaintext, key);
    const value = store.decodeCookieValue(
      { hostKey: ".example.com", value: "", encryptedValue: encrypted },
      key,
    );
    assert.equal(value, plaintext.toString("utf8"));
  });

  it("returns the unencrypted value when Chrome stored one", () => {
    const value = store.decodeCookieValue(
      { hostKey: ".example.com", value: "legacy", encryptedValue: Buffer.alloc(0) },
      key,
    );
    assert.equal(value, "legacy");
  });
});

describe("chromeCookieStore — Electron cookie mapping", () => {
  it("maps a domain cookie to an explicit domain + https URL", () => {
    const cookie = store.toElectronCookie({
      hostKey: ".example.com",
      name: "SID",
      path: "/mail",
      secure: true,
      httpOnly: true,
      sameSite: 1,
      expiresUtc: chromeExpiresUtc(1_900_000_000),
    });
    assert.deepEqual(cookie, {
      url: "https://example.com/mail",
      domain: ".example.com",
      name: "SID",
      value: undefined,
      path: "/mail",
      secure: true,
      httpOnly: true,
      sameSite: "lax",
      expirationDate: 1_900_000_000,
    });
  });

  it("leaves host-only cookies without a domain and omits session expiry", () => {
    const cookie = store.toElectronCookie({
      hostKey: "example.com",
      name: "session",
      path: "",
      secure: false,
      httpOnly: false,
      sameSite: -1,
      expiresUtc: 0,
    });
    assert.equal(cookie.domain, undefined);
    assert.equal(cookie.path, "/");
    assert.equal(cookie.expirationDate, undefined);
    assert.equal(cookie.sameSite, "unspecified");
  });

  it("brackets IPv6 hosts so cookies.set accepts the URL", () => {
    const cookie = store.toElectronCookie({
      hostKey: "::1",
      name: "ipv6",
      path: "/",
      secure: false,
      httpOnly: false,
      sameSite: -1,
      expiresUtc: 0,
    });
    assert.equal(cookie.url, "https://[::1]/");
  });

  it("maps every Chromium sameSite code", () => {
    const codes = [-1, 0, 1, 2];
    assert.deepEqual(
      codes.map(
        (sameSite) =>
          store.toElectronCookie({
            hostKey: "example.com",
            name: "n",
            path: "/",
            sameSite,
            expiresUtc: 0,
          }).sameSite,
      ),
      ["unspecified", "no_restriction", "lax", "strict"],
    );
  });

  it("plans only decryptable, in-scope, unexpired cookies", () => {
    const key = store.deriveMacKey("test-password");
    const rows = [
      {
        hostKey: ".example.com",
        name: "good",
        path: "/",
        encryptedValue: encryptChromeValue(boundPlaintext(".example.com", "v1"), key),
        expiresUtc: 0,
      },
      {
        // Encrypted with a different key: Chrome under another login.
        hostKey: ".example.com",
        name: "foreign-key",
        path: "/",
        encryptedValue: encryptChromeValue(Buffer.from("nope"), store.deriveMacKey("other")),
        expiresUtc: 0,
      },
      {
        hostKey: ".example.com",
        name: "expired",
        path: "/",
        encryptedValue: Buffer.alloc(0),
        value: "old",
        expiresUtc: chromeExpiresUtc(Date.now() / 1000 - 60),
      },
      {
        hostKey: ".elsewhere.com",
        name: "other-site",
        path: "/",
        encryptedValue: Buffer.alloc(0),
        value: "nope",
        expiresUtc: 0,
      },
    ];
    const plan = store.buildCookiePlan({ rows, scope: "example.com", key });
    assert.equal(plan.selectedCount, 2);
    assert.equal(plan.skippedCount, 1);
    assert.deepEqual(
      plan.cookies.map((cookie) => `${cookie.name}=${cookie.value}`),
      ["good=v1"],
    );
  });
});

describe("chromeCookieStore — system binaries", () => {
  it("runs sqlite3 and security by absolute path", async () => {
    const calls = [];
    const failing = async (command) => {
      calls.push(command);
      throw new Error("boom");
    };
    // A fake snapshot keeps this test independent of a real sqlite3 binary.
    const fsImpl = {
      promises: {
        mkdtemp: async () => path.join(os.tmpdir(), "omnigent-chrome-cookie-fake"),
        copyFile: async () => {},
        rm: async () => {},
      },
    };
    await assert.rejects(
      store.readCookieRows({
        cookieDbPath: "/nonexistent/Cookies",
        scopes: ["example.com"],
        run: failing,
        fsImpl,
      }),
      (err) => {
        // Fixed text: the exec error would carry the command line and temp paths.
        assert.equal(err.message, "Could not read Chrome's cookie database.");
        assert.doesNotMatch(err.message, /sqlite3/);
        return true;
      },
    );
    calls.length = 0;
    const password = await store.readSafeStoragePassword({
      run: async (command) => {
        calls.push(command);
        return { stdout: "secret\n" };
      },
    });
    assert.equal(password, "secret");
    assert.deepEqual(calls, ["/usr/bin/security"]);
  });
});

describe("chromeCookieStore — Keychain", () => {
  it("returns the password without its trailing newline", async () => {
    const password = await store.readSafeStoragePassword({
      run: async () => ({ stdout: "horse-battery\n" }),
    });
    assert.equal(password, "horse-battery");
  });

  it("explains a missing Keychain item", async () => {
    await assert.rejects(
      store.readSafeStoragePassword({
        run: async () => {
          throw Object.assign(new Error("item not found"), { code: 44 });
        },
      }),
      /isn't in your login Keychain/,
    );
  });

  it("explains a dismissed/timed-out approval prompt", async () => {
    await assert.rejects(
      store.readSafeStoragePassword({
        run: async () => {
          throw Object.assign(new Error("killed"), { killed: true, signal: "SIGTERM" });
        },
      }),
      /Timed out waiting for Keychain access/,
    );
  });

  it("never echoes the security(1) error text", async () => {
    await assert.rejects(
      store.readSafeStoragePassword({
        run: async () => {
          throw Object.assign(
            new Error(
              "security: SecKeychainSearchCopyNext: The user name or passphrase you entered is not correct.",
            ),
            {
              code: 51,
            },
          );
        },
      }),
      (err) => {
        assert.match(err.message, /Couldn't read Chrome's cookie key/);
        assert.doesNotMatch(err.message, /SecKeychain/);
        return true;
      },
    );
  });
});

describe("chromeCookieStore — Chrome profile discovery", () => {
  function writeProfile(root, dir, { localState, withCookies = true } = {}) {
    fs.mkdirSync(path.join(root, dir), { recursive: true });
    if (withCookies) fs.writeFileSync(path.join(root, dir, "Cookies"), "");
    if (localState) fs.writeFileSync(path.join(root, "Local State"), JSON.stringify(localState));
  }

  it("orders profiles by last_used and skips ones without a cookie DB", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-profiles-"));
    try {
      writeProfile(root, "Default", {
        localState: {
          profile: {
            last_used: "Profile 2",
            info_cache: {
              Default: { name: "Work" },
              "Profile 2": { name: "Personal" },
              "Guest Profile": { name: "Guest" },
            },
          },
        },
      });
      writeProfile(root, "Profile 2");
      writeProfile(root, "Guest Profile", { withCookies: false });
      const profiles = await store.listChromeProfiles({ rootDir: root });
      assert.deepEqual(
        profiles.map((profile) => [profile.id, profile.name]),
        [
          ["Profile 2", "Personal"],
          ["Default", "Work"],
        ],
      );
      assert.equal(profiles[0].cookieDbPath, path.join(root, "Profile 2", "Cookies"));
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("returns nothing when there is no Chrome install", async () => {
    assert.deepEqual(await store.listChromeProfiles({ rootDir: "/nonexistent/chrome" }), []);
  });
});

describe("chromeCookieStore — import scopes", () => {
  it("offers the page host plus each parent domain, never a bare TLD", () => {
    assert.deepEqual(store.cookieScopeCandidates("mail.google.com"), [
      "mail.google.com",
      "google.com",
    ]);
    assert.deepEqual(store.cookieScopeCandidates("x.y.z"), ["x.y.z", "y.z"]);
    assert.deepEqual(store.cookieScopeCandidates("a.b.c.d"), ["a.b.c.d", "b.c.d", "c.d"]);
    // Nothing above the registrable-looking pair, and no TLD on its own.
    assert.deepEqual(store.cookieScopeCandidates("example.com"), ["example.com"]);
    // IP literals have no parent domains.
    assert.deepEqual(store.cookieScopeCandidates("127.0.0.1"), ["127.0.0.1"]);
  });

  it("counts the site's own cookies when no clock is passed", () => {
    const rows = [
      { hostKey: ".example.com", name: "shared", expiresUtc: chromeExpiresUtc(1_900_000_000) },
    ];
    assert.equal(store.selectCookiesForScope(rows, "mail.example.com").length, 1);
  });
});

describe("chromeCookieStore — sqlite3 reader", { skip: !hasSqlite3 }, () => {
  const key = store.deriveMacKey("test-password");

  function makeCookieDb(databasePath, { legacyColumns = false } = {}) {
    // Chrome renamed secure/httponly and added samesite in 2016; a legacy
    // profile must still read through the fallback column set.
    const scopeColumns = legacyColumns
      ? "secure INTEGER NOT NULL DEFAULT 0, httponly INTEGER NOT NULL DEFAULT 0"
      : "is_secure INTEGER NOT NULL DEFAULT 0, is_httponly INTEGER NOT NULL DEFAULT 0, samesite INTEGER NOT NULL DEFAULT -1";
    const insertCookie = (hostKey, name, encrypted, scope) =>
      `INSERT INTO cookies (host_key, name, path, encrypted_value, expires_utc, ${legacyColumns ? "secure, httponly" : "is_secure, is_httponly, samesite"})
        VALUES ('${hostKey}', '${name}', '/', X'${encrypted.toString("hex")}',
          ${chromeExpiresUtc(1_900_000_000)}, ${scope});`;
    const secureScope = legacyColumns ? "1, 1" : "1, 1, 1";
    const plainScope = legacyColumns ? "0, 0" : "0, 0, -1";
    const sql = [
      "PRAGMA journal_mode=WAL;",
      `CREATE TABLE cookies (
        host_key TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL,
        value TEXT NOT NULL DEFAULT '', encrypted_value BLOB NOT NULL DEFAULT X'',
        expires_utc INTEGER NOT NULL DEFAULT 0, has_expires INTEGER NOT NULL DEFAULT 1,
        ${scopeColumns}, is_persistent INTEGER NOT NULL DEFAULT 1
      );`,
      insertCookie(
        ".example.com",
        "SID",
        encryptChromeValue(boundPlaintext(".example.com", "sid-value"), key),
        secureScope,
      ),
      insertCookie(
        "files.example.com",
        "hostonly",
        encryptChromeValue(Buffer.from("host-value"), key),
        plainScope,
      ),
      insertCookie(
        "example.com",
        "parentonly",
        encryptChromeValue(Buffer.from("parent-value"), key),
        plainScope,
      ),
      insertCookie(
        ".unrelated.test",
        "nope",
        encryptChromeValue(Buffer.from("other"), key),
        plainScope,
      ),
    ].join("\n");
    execFileSync("sqlite3", [databasePath], { input: sql });
  }

  it("snapshots and reads only the rows that can apply to the host", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-db-"));
    try {
      const databasePath = path.join(root, "Cookies");
      makeCookieDb(databasePath);
      const tempDirsBefore = fs
        .readdirSync(os.tmpdir())
        .filter((name) => name.startsWith("omnigent-chrome-cookie-"));
      const rows = await store.readCookieRows({
        cookieDbPath: databasePath,
        scopes: ["files.example.com"],
      });
      assert.deepEqual(rows.map((row) => row.name).sort(), ["SID", "hostonly"]);
      const sid = rows.find((row) => row.name === "SID");
      assert.equal(sid.hostKey, ".example.com");
      assert.equal(sid.secure, true);
      assert.equal(sid.httpOnly, true);
      assert.equal(sid.sameSite, 1);
      assert.equal(store.decodeCookieValue(sid, key), "sid-value");
      // The temp snapshot must not be left behind.
      assert.deepEqual(
        fs.readdirSync(os.tmpdir()).filter((name) => name.startsWith("omnigent-chrome-cookie-")),
        tempDirsBefore,
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("falls back to the legacy secure/httponly columns", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-legacy-"));
    try {
      const databasePath = path.join(root, "Cookies");
      makeCookieDb(databasePath, { legacyColumns: true });
      const rows = await store.readCookieRows({
        cookieDbPath: databasePath,
        scopes: ["example.com"],
      });
      // `example.com`'s tree: its own host-only cookie, the shared one, and
      // files.example.com's host-only cookie.
      assert.equal(rows.length, 3);
      const shared = rows.find((row) => row.hostKey === ".example.com");
      assert.equal(shared.secure, true);
      assert.equal(shared.httpOnly, true);
      // No samesite column: Chrome's pre-2016 default.
      assert.equal(shared.sameSite, -1);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  function makeScopeDb(databasePath, entries) {
    const statements = [
      "PRAGMA journal_mode=WAL;",
      `CREATE TABLE cookies (
        host_key TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL,
        value TEXT NOT NULL DEFAULT '', encrypted_value BLOB NOT NULL DEFAULT X'',
        expires_utc INTEGER NOT NULL DEFAULT 0, is_secure INTEGER NOT NULL DEFAULT 0,
        is_httponly INTEGER NOT NULL DEFAULT 0, samesite INTEGER NOT NULL DEFAULT -1,
        is_persistent INTEGER NOT NULL DEFAULT 1, top_frame_site_key TEXT NOT NULL DEFAULT ''
      );`,
      ...entries.map(
        ([hostKey, name, partitionKey]) =>
          `INSERT INTO cookies (host_key, name, path, top_frame_site_key)
            VALUES ('${hostKey}', '${name}', '/', '${partitionKey ?? ""}');`,
      ),
    ];
    execFileSync("sqlite3", [databasePath], { input: statements.join("\n") });
  }

  it("never offers a scope Chrome holds no domain cookie for", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-psl-"));
    try {
      fs.mkdirSync(path.join(root, "Default"));
      const databasePath = path.join(root, "Default", "Cookies");
      // Chrome refuses to store `.co.uk` / `.github.io`: those are public
      // suffixes, so the candidates list must never surface them as a scope.
      makeScopeDb(databasePath, [
        [".example.co.uk", "shared"],
        ["mail.example.co.uk", "hostonly"],
        [".other.co.uk", "neighbour"],
        ["foo.github.io", "pages"],
      ]);
      const { scopes, rows } = await store.readScopeTrees({
        cookieDbPath: databasePath,
        host: "mail.example.co.uk",
      });
      assert.deepEqual(scopes, ["mail.example.co.uk", "example.co.uk"]);
      // Only the site's own tree was read, so the neighbouring site's cookie
      // never enters the process.
      assert.deepEqual(rows.map((row) => row.name).sort(), ["hostonly", "shared"]);
      const plain = await store.readScopeTrees({
        cookieDbPath: databasePath,
        host: "foo.github.io",
      });
      assert.deepEqual(plain.scopes, ["foo.github.io"]);

      const written = [];
      const result = await store.importSiteCookies({
        host: "mail.example.co.uk",
        scope: "co.uk",
        profileId: "Default",
        cookiesApi: { set: async (cookie) => written.push(cookie) },
        rootDir: root,
        readPassword: async () => "test-password",
      });
      assert.equal(result.ok, false);
      assert.match(result.error, /scope isn't available/);
      assert.deepEqual(written, []);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("skips partitioned (CHIPS) cookies instead of widening them", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-chips-"));
    try {
      fs.mkdirSync(path.join(root, "Default"));
      const databasePath = path.join(root, "Default", "Cookies");
      makeScopeDb(databasePath, [
        ["example.com", "unpartitioned"],
        ["example.com", "partitioned", "https://top.example"],
      ]);
      const { rows } = await store.readScopeTrees({
        cookieDbPath: databasePath,
        host: "example.com",
      });
      assert.deepEqual(
        rows.map((row) => row.name),
        ["unpartitioned"],
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("counts per profile for the preview without needing decryption", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-preview-"));
    try {
      fs.mkdirSync(path.join(root, "Default"));
      makeCookieDb(path.join(root, "Default", "Cookies"));
      fs.mkdirSync(path.join(root, "Profile 2"));
      // A second profile with no cookies for this site is not offered.
      makeCookieDb(path.join(root, "Profile 2", "Cookies"));
      fs.writeFileSync(
        path.join(root, "Local State"),
        JSON.stringify({
          profile: {
            last_used: "Profile 2",
            info_cache: { Default: { name: "Work" }, "Profile 2": { name: "Personal" } },
          },
        }),
      );
      const preview = await store.previewSiteCookies({ host: "mail.example.com", rootDir: root });
      assert.equal(preview.ok, true);
      assert.equal(preview.host, "mail.example.com");
      assert.deepEqual(
        preview.profiles.map((profile) => [profile.id, profile.cookieCount]),
        [
          // last_used first; only the shared `.example.com` cookie scopes to
          // `mail.example.com` (a host-only cookie for files.example.com doesn't).
          ["Profile 2", 1],
          ["Default", 1],
        ],
      );
      assert.deepEqual(preview.scopes, ["mail.example.com", "example.com"]);
      // The parent scope covers everything under example.com, so it can only
      // add to the narrower one.
      assert.deepEqual(preview.profiles[0].scopes, [
        { host: "mail.example.com", cookieCount: 1 },
        { host: "example.com", cookieCount: 3 },
      ]);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("imports the site's cookies into the given session and reports counts", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-import-"));
    try {
      fs.mkdirSync(path.join(root, "Default"));
      makeCookieDb(path.join(root, "Default", "Cookies"));
      const written = [];
      const result = await store.importSiteCookies({
        host: "files.example.com",
        profileId: "Default",
        cookiesApi: { set: async (cookie) => written.push(cookie) },
        rootDir: root,
        readPassword: async () => "test-password",
      });
      assert.equal(result.ok, true);
      assert.equal(result.found, 2);
      assert.equal(result.imported, 2);
      assert.equal(result.skipped, 0);
      assert.equal(result.failed, 0);
      assert.deepEqual(written.map((cookie) => [cookie.name, cookie.value, cookie.domain]).sort(), [
        ["SID", "sid-value", ".example.com"],
        ["hostonly", "host-value", undefined],
      ]);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("imports a parent domain when the user picks that scope", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-scope-"));
    try {
      fs.mkdirSync(path.join(root, "Default"));
      makeCookieDb(path.join(root, "Default", "Cookies"));
      const written = [];
      const result = await store.importSiteCookies({
        host: "files.example.com",
        scope: "example.com",
        profileId: "Default",
        cookiesApi: { set: async (cookie) => written.push(cookie) },
        rootDir: root,
        readPassword: async () => "test-password",
      });
      assert.equal(result.ok, true);
      assert.equal(result.host, "files.example.com");
      assert.equal(result.scope, "example.com");
      // example.com's tree: its own host-only cookie, the shared one, and the
      // subdomain's host-only cookie the user asked to include.
      assert.deepEqual(written.map((cookie) => cookie.name).sort(), [
        "SID",
        "hostonly",
        "parentonly",
      ]);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("refuses a scope that is not the page's host or its parent", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-badscope-"));
    try {
      fs.mkdirSync(path.join(root, "Default"));
      makeCookieDb(path.join(root, "Default", "Cookies"));
      const written = [];
      const result = await store.importSiteCookies({
        host: "files.example.com",
        scope: "unrelated.test",
        profileId: "Default",
        cookiesApi: { set: async (cookie) => written.push(cookie) },
        rootDir: root,
        readPassword: async () => "test-password",
      });
      assert.equal(result.ok, false);
      assert.match(result.error, /scope isn't available/);
      assert.deepEqual(written, []);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("reports a keychain failure instead of throwing", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-keychain-"));
    try {
      fs.mkdirSync(path.join(root, "Default"));
      makeCookieDb(path.join(root, "Default", "Cookies"));
      const result = await store.importSiteCookies({
        host: "example.com",
        profileId: "Default",
        cookiesApi: { set: async () => {} },
        rootDir: root,
        readPassword: async () => {
          throw new Error("Timed out waiting for Keychain access. Retry and approve the prompt.");
        },
      });
      assert.equal(result.ok, false);
      assert.match(result.error, /Timed out waiting for Keychain access/);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("writes nothing when the tab leaves the site before the write", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-moved-"));
    try {
      fs.mkdirSync(path.join(root, "Default"));
      makeCookieDb(path.join(root, "Default", "Cookies"));
      const written = [];
      const result = await store.importSiteCookies({
        host: "files.example.com",
        profileId: "Default",
        cookiesApi: { set: async (cookie) => written.push(cookie) },
        rootDir: root,
        readPassword: async () => "test-password",
        beforeWrite: () => false,
      });
      assert.equal(result.ok, false);
      assert.match(result.error, /no longer on that site/);
      assert.deepEqual(written, []);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("counts cookies the session rejects instead of failing the import", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-reject-"));
    try {
      fs.mkdirSync(path.join(root, "Default"));
      makeCookieDb(path.join(root, "Default", "Cookies"));
      const result = await store.importSiteCookies({
        host: "files.example.com",
        profileId: "Default",
        cookiesApi: {
          set: async (cookie) => {
            if (cookie.name === "hostonly") throw new Error("rejected by the session");
          },
        },
        rootDir: root,
        readPassword: async () => "test-password",
      });
      assert.equal(result.ok, true);
      assert.equal(result.imported, 1);
      assert.equal(result.failed, 1);
      assert.equal(result.skipped, 0);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("reports an unreadable database without echoing the command line", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-broken-"));
    try {
      fs.mkdirSync(path.join(root, "Default"));
      fs.writeFileSync(path.join(root, "Default", "Cookies"), "not a database");
      // A parent-domain host runs the ancestor query first, the read that used
      // to escape unwrapped.
      await assert.rejects(
        store.importSiteCookies({
          host: "www.example.com",
          profileId: "Default",
          cookiesApi: { set: async () => {} },
          rootDir: root,
          readPassword: async () => "test-password",
        }),
        (err) => {
          assert.equal(err.message, "Could not read Chrome's cookie database.");
          assert.doesNotMatch(err.message, /sqlite3|omnigent-chrome-cookie-|\/Users\//);
          return true;
        },
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("reports an unknown profile instead of importing", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-chrome-noprofile-"));
    try {
      fs.mkdirSync(path.join(root, "Default"));
      makeCookieDb(path.join(root, "Default", "Cookies"));
      const result = await store.importSiteCookies({
        host: "example.com",
        profileId: "Profile 9",
        cookiesApi: { set: async () => {} },
        rootDir: root,
        readPassword: async () => "test-password",
      });
      assert.equal(result.ok, false);
      assert.match(result.error, /wasn't found/);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});
