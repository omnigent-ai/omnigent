// Read the current site's cookies out of a local Google Chrome install and
// replay them into one embedded browser session. Chrome owns the database; this
// module reads a private copy of it and never writes to Chrome's file. Cookie
// values never leave here except as `session.cookies.set()` payloads.
//
// Everything is injectable (`run`, `fsImpl`, `readPassword`, `cookiesApi`,
// `rootDir`, `nowMs`) so tests drive it without Chrome, a Keychain or Electron.
// See `web/electron/README.md` for the user-facing behaviour.

import { execFile } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

/** Runs a system binary; `execFile` promisified. Its stdout is untrusted. */
export type RunCommand = (
  file: string,
  args: string[],
  options?: { timeout: number },
) => Promise<{ stdout: unknown }>;

/** The file operations that snapshot a cookie database. */
export interface SnapshotFs {
  promises: {
    mkdtemp: (prefix: string) => Promise<string>;
    copyFile: (source: string, destination: string) => Promise<void>;
    rm: (target: string, options: { recursive: boolean; force: boolean }) => Promise<void>;
  };
}

/** The file operations that discover Chrome profiles. */
export interface ProfileFs {
  promises: {
    readFile: (file: string, encoding: "utf8") => Promise<string>;
    readdir: (
      directory: string,
      options: { withFileTypes: true },
    ) => Promise<{ name: string; isDirectory: () => boolean }[]>;
  };
  existsSync: (file: string) => boolean;
}

export type ChromeFs = SnapshotFs & ProfileFs;

/** Electron's cookie `sameSite` values. */
export type CookieSameSite = "unspecified" | "no_restriction" | "lax" | "strict";

/** A Chrome `cookies` row after `normalizeRow`. */
export interface CookieRow {
  hostKey: string;
  name: string;
  path: string;
  value: string;
  encryptedValue: Buffer;
  expiresUtc: number;
  secure: boolean;
  httpOnly: boolean;
  sameSite: number;
  partitionKey: string;
}

/** The row fields `decodeCookieValue` reads. */
export interface EncryptedCookieFields {
  hostKey: string;
  value?: string;
  encryptedValue?: Buffer;
}

/** The row fields `toElectronCookie` reads. */
export interface ElectronCookieFields {
  hostKey: string;
  name: string;
  path?: string;
  secure?: boolean;
  httpOnly?: boolean;
  sameSite?: number;
  expiresUtc?: number;
}

/** A `session.cookies.set` payload. */
export interface ElectronCookie {
  url: string;
  name: string;
  value: string;
  path: string;
  secure: boolean;
  domain?: string;
  httpOnly?: boolean;
  sameSite?: CookieSameSite;
  expirationDate?: number;
}

export interface ChromeProfile {
  id: string;
  name: string;
  cookieDbPath: string;
}

/** A property of a JSON value, read the way `value?.[key]` would. */
function propertyOf(value: unknown, key: string): unknown {
  return value == null ? undefined : (value as Record<string, unknown>)[key];
}

/** Chrome's encryption key lives under this Keychain generic password. */
export const SAFE_STORAGE_SERVICE = "Chrome Safe Storage";

// Absolute paths: a GUI-launched shell has a minimal PATH, and a PATH lookup
// would let a planted binary stand in for the one that reads Chrome's key.
const SQLITE3_BINARY = "/usr/bin/sqlite3";
const SECURITY_BINARY = "/usr/bin/security";
/** Chrome's PBKDF2 parameters for the "v10" AES key (unchanged since 2013). */
export const KEY_SALT = "saltysalt";
export const KEY_ITERATIONS = 1003;
export const KEY_LENGTH = 16;
/** The Keychain read blocks on a system approval dialog the first time. */
const KEYCHAIN_TIMEOUT_MS = 120_000;
/** Seconds between the Windows/Chrome epoch (1601-01-01) and the Unix epoch. */
const CHROME_EPOCH_OFFSET_SECONDS = 11644473600;
/** Chrome 130+ prepends SHA-256(host_key) to the plaintext (domain binding). */
const DOMAIN_HASH_LENGTH = 32;

const COOKIE_DB_READ_ERROR = "Could not read Chrome's cookie database.";

// Chrome renamed `secure`/`httponly` to `is_secure`/`is_httponly` and added
// `samesite` in 2016. Try the modern set first, then the legacy one, so a very
// old (or renamed) profile still reads instead of failing outright.
const COOKIE_COLUMNS = "host_key, name, path, value, hex(encrypted_value) AS encrypted_hex";
const COLUMN_SETS = [
  // `top_frame_site_key` (CHIPS) only exists in newer schemas, so it heads its
  // own attempt instead of breaking the read on an older profile.
  `${COOKIE_COLUMNS}, expires_utc, is_secure, is_httponly, samesite, top_frame_site_key`,
  `${COOKIE_COLUMNS}, expires_utc, is_secure, is_httponly, samesite`,
  `${COOKIE_COLUMNS}, expires_utc, secure, httponly`,
];

const SAME_SITE_BY_CODE: Record<string, CookieSameSite | undefined> = {
  "-1": "unspecified",
  0: "no_restriction",
  1: "lax",
  2: "strict",
};

/** Chrome's per-user profile root on macOS. */
export function defaultChromeRoot(): string {
  return path.join(os.homedir(), "Library", "Application Support", "Google", "Chrome");
}

function messageOf(err: unknown): string {
  if (!err) return "unknown error";
  if (typeof err === "string") return err;
  const message = propertyOf(err, "message");
  return message ? String(message) : String(err);
}

function escapeSqlLiteral(value: string): string {
  return String(value).replace(/'/g, "''");
}

function numberFlag(value: unknown): boolean {
  if (value === true || value === "1") return true;
  return Number(value) === 1;
}

/**
 * Lowercase a URL hostname into the form Chrome stores in `host_key`: no
 * trailing dot, no IPv6 brackets. Throws on anything that isn't a hostname so a
 * malformed host can never reach the SQL literal.
 *
 * @param host A URL hostname, possibly from the renderer.
 */
export function normalizeHost(host: unknown): string {
  const raw = String(host ?? "")
    .trim()
    .toLowerCase();
  const unwrapped = raw.startsWith("[") && raw.endsWith("]") ? raw.slice(1, -1) : raw;
  const trimmed = unwrapped.replace(/\.$/, "");
  if (!trimmed || trimmed.length > 253 || !/^[a-z0-9.:_-]+$/.test(trimmed)) {
    throw new Error("unsupported host");
  }
  return trimmed;
}

/**
 * The scopes a user may pick from, widest last: the page's own host, then each
 * parent domain that could hold cookies for it (`mail.google.com` →
 * `mail.google.com`, `google.com`). A bare TLD never hosts cookies, and an IP
 * literal has no parent domain.
 *
 */
export function cookieScopeCandidates(host: unknown): string[] {
  const normalized = normalizeHost(host);
  const labels = normalized.split(".");
  const isIpv6 = normalized.includes(":");
  const isIpv4 = !isIpv6 && labels.every((label) => /^\d+$/.test(label));
  if (isIpv6 || isIpv4) return [normalized];
  const scopes = [normalized];
  for (let i = 1; i < labels.length - 1; i += 1) scopes.push(labels.slice(i).join("."));
  return scopes;
}

/**
 * Whether a Chrome `host_key` belongs to `scope`'s tree: the scope itself, any
 * subdomain of it, and the dot-prefixed domain cookies for either. Cookies never
 * travel upwards, so `accounts.google.com` belongs to `google.com` but
 * `mail.google.com` does not belong to `accounts.google.com`.
 *
 */
export function cookieAppliesToScope(hostKey: string | null | undefined, scope: string): boolean {
  const key = String(hostKey ?? "").replace(/^\./, "");
  if (!key || !scope) return false;
  return key === scope || key.endsWith(`.${scope}`);
}

/**
 * Dotted domain keys that apply down to `scope` from above (`.google.com` for
 * `mail.google.com`). Host-only cookies are excluded: they never travel down.
 *
 */
function cookieAncestorDomains(scope: string): string[] {
  const labels = scope.split(".");
  const ancestors: string[] = [];
  for (let i = 1; i < labels.length - 1; i += 1) ancestors.push(`.${labels.slice(i).join(".")}`);
  return ancestors;
}

/**
 * Whether a row belongs to `scope`: anywhere in its tree, or inherited from an
 * ancestor's domain cookie. Chosen so widening the scope only adds cookies.
 *
 */
export function cookieInScope(hostKey: string | null | undefined, scope: string): boolean {
  if (cookieAppliesToScope(hostKey, scope)) return true;
  const key = String(hostKey ?? "");
  return key.startsWith(".") && cookieAppliesToScope(scope, key.slice(1));
}

/** Escape a value for a SQLite `LIKE ... ESCAPE '\\'` pattern. */
function escapeLike(value: string): string {
  return String(value).replace(/[\\%_]/g, (char) => `\\${char}`);
}

export function chromeExpirySeconds(expiresUtc: unknown): number | undefined {
  const micros = Number(expiresUtc);
  if (!Number.isFinite(micros) || micros <= 0) return undefined;
  const seconds = micros / 1e6 - CHROME_EPOCH_OFFSET_SECONDS;
  return seconds > 0 ? seconds : undefined;
}

function sameSiteValue(code: number | null | undefined): CookieSameSite | undefined {
  const key = code == null ? "-1" : String(code);
  return SAME_SITE_BY_CODE[key];
}

/**
 * Drop cookies outside the scope's tree or already expired. Session cookies (no
 * expiry) are kept.
 *
 * @param rows normalized rows from `readCookieRows`
 */
export function selectCookiesForScope<Row extends { hostKey: string; expiresUtc?: number }>(
  rows: readonly Row[] | null | undefined,
  scope: string,
  nowMs: number = Date.now(),
): Row[] {
  const normalized = normalizeHost(scope);
  const nowSeconds = nowMs / 1000;
  return (rows ?? []).filter((row) => {
    if (!cookieInScope(row.hostKey, normalized)) return false;
    const expiry = chromeExpirySeconds(row.expiresUtc);
    return expiry === undefined || expiry > nowSeconds;
  });
}

/** AES-128-CBC key Chrome derives from the Keychain password. */
export function deriveMacKey(password: string): Buffer {
  return crypto.pbkdf2Sync(String(password), KEY_SALT, KEY_ITERATIONS, KEY_LENGTH, "sha1");
}

/**
 * Decrypt a Chrome `encrypted_value` (the `v10`/`v11` AES-128-CBC envelope,
 * IV = 16 spaces). Returns raw bytes; see `decodeCookieValue` for the plaintext.
 *
 */
export function decryptChromeValue(encryptedValue: Buffer | null | undefined, key: Buffer): Buffer {
  if (!encryptedValue || encryptedValue.length === 0) {
    throw new Error("empty encrypted value");
  }
  const prefix = encryptedValue.subarray(0, 3).toString("latin1");
  if (prefix !== "v10" && prefix !== "v11") {
    // v20 (app-bound encryption) is Windows-only today; anything else means a
    // Chrome build whose scheme we don't understand — skip rather than guess.
    throw new Error(
      `unsupported cookie encryption (${prefix.replace(/[^a-z0-9]/gi, "") || "none"})`,
    );
  }
  const decipher = crypto.createDecipheriv("aes-128-cbc", key, Buffer.alloc(16, 0x20));
  return Buffer.concat([decipher.update(encryptedValue.subarray(3)), decipher.final()]);
}

/**
 * Chrome 130+ binds a value to its host by prefixing SHA-256(host_key); strip it
 * when it matches so the cookie value itself comes back intact.
 */
function stripDomainHash(plaintext: Buffer, hostKey: string): Buffer {
  // No length guard: an empty value is exactly the prefix, and a shorter buffer
  // fails the comparison anyway.
  const expected = crypto.createHash("sha256").update(hostKey, "utf8").digest();
  return plaintext.subarray(0, DOMAIN_HASH_LENGTH).equals(expected)
    ? plaintext.subarray(DOMAIN_HASH_LENGTH)
    : plaintext;
}

/**
 * The cookie's plaintext value. Chrome stores unencrypted values (rare, mostly
 * restored/pre-2013 rows) in `value` with an empty `encrypted_value`.
 *
 */
export function decodeCookieValue(row: EncryptedCookieFields, key: Buffer): string {
  if (!row.encryptedValue || row.encryptedValue.length === 0) return row.value ?? "";
  const plaintext = stripDomainHash(decryptChromeValue(row.encryptedValue, key), row.hostKey);
  return plaintext.toString("utf8");
}

/**
 * Map a Chrome row + its plaintext value onto `session.cookies.set` args. The
 * URL is always https so `secure`/`__Host-` cookies are accepted; the cookie's
 * own domain/path scope is carried explicitly.
 *
 */
export function toElectronCookie(row: ElectronCookieFields, value: string): ElectronCookie {
  const isDomainCookie = row.hostKey.startsWith(".");
  const host = isDomainCookie ? row.hostKey.slice(1) : row.hostKey;
  // IPv6 literals need brackets in a URL, or `cookies.set` rejects the cookie.
  const urlHost = host.includes(":") ? `[${host}]` : host;
  const cookiePath = row.path && row.path.startsWith("/") ? row.path : "/";
  const cookie: ElectronCookie = {
    url: `https://${urlHost}${cookiePath}`,
    name: row.name,
    value,
    path: cookiePath,
    secure: !!row.secure,
  };
  if (isDomainCookie) cookie.domain = row.hostKey;
  if (row.httpOnly) cookie.httpOnly = true;
  const sameSite = sameSiteValue(row.sameSite);
  if (sameSite) cookie.sameSite = sameSite;
  const expires = chromeExpirySeconds(row.expiresUtc);
  if (expires !== undefined) cookie.expirationDate = expires;
  return cookie;
}

/**
 * Turn matching Chrome rows into `session.cookies.set` payloads, collecting the
 * ones we can't decrypt instead of failing the whole import.
 *
 */
export function buildCookiePlan({
  rows,
  scope,
  key,
  nowMs = Date.now(),
}: {
  rows: readonly (EncryptedCookieFields & ElectronCookieFields)[];
  scope: string;
  key: Buffer;
  nowMs?: number;
}): { cookies: ElectronCookie[]; skippedCount: number; selectedCount: number } {
  const selected = selectCookiesForScope(rows, scope, nowMs);
  const cookies: ElectronCookie[] = [];
  let skippedCount = 0;
  for (const row of selected) {
    try {
      cookies.push(toElectronCookie(row, decodeCookieValue(row, key)));
    } catch {
      // Undecryptable (foreign key, unknown scheme) or nameless row: skip it and
      // report a count — never a value or a host in the message.
      skippedCount += 1;
    }
  }
  return { cookies, skippedCount, selectedCount: selected.length };
}

/** JSON rows out of `sqlite3`, tolerating the CLI's empty-output-for-no-rows. */
function parseSqliteJson(stdout: unknown): Record<string, unknown>[] {
  const text = String(stdout ?? "").trim();
  if (!text) return [];
  const parsed: unknown = JSON.parse(text);
  // `sqlite3 -json` prints an array of row objects; their values stay unknown.
  return Array.isArray(parsed) ? (parsed as Record<string, unknown>[]) : [];
}

export function normalizeRow(raw: Record<string, unknown>): CookieRow {
  return {
    hostKey: String(raw.host_key ?? ""),
    name: String(raw.name ?? ""),
    path: String(raw.path ?? "/"),
    value: typeof raw.value === "string" ? raw.value : "",
    encryptedValue:
      typeof raw.encrypted_hex === "string" && raw.encrypted_hex
        ? Buffer.from(raw.encrypted_hex, "hex")
        : Buffer.alloc(0),
    expiresUtc: Number(raw.expires_utc ?? 0) || 0,
    secure: numberFlag(raw.is_secure ?? raw.secure),
    httpOnly: numberFlag(raw.is_httponly ?? raw.httponly),
    sameSite: raw.samesite == null ? -1 : Number(raw.samesite),
    // Non-empty for a partitioned (CHIPS) cookie. Those are replayed without
    // their partition, which would widen them, so the read drops them.
    partitionKey: String(raw.top_frame_site_key ?? ""),
  };
}

/**
 * Read Chrome's profiles out of `Local State` (name + last-used order), falling
 * back to scanning for profile dirs that hold a cookie DB.
 *
 */
export async function listChromeProfiles({
  rootDir = defaultChromeRoot(),
  fsImpl = fs,
}: { rootDir?: string | undefined; fsImpl?: ProfileFs } = {}): Promise<ChromeProfile[]> {
  const state = await fsImpl.promises
    .readFile(path.join(rootDir, "Local State"), "utf8")
    .then((text): unknown => JSON.parse(text))
    .catch(() => null);
  const infoCache = propertyOf(propertyOf(state, "profile"), "info_cache") ?? {};
  const lastUsed = propertyOf(propertyOf(state, "profile"), "last_used");
  let dirs = Object.keys(infoCache).filter(
    (dir) => dir && dir !== "." && dir !== ".." && !/[\\/]/.test(dir),
  );
  if (dirs.length === 0) {
    try {
      dirs = (await fsImpl.promises.readdir(rootDir, { withFileTypes: true }))
        .filter((entry) => entry.isDirectory())
        .map((entry) => entry.name);
    } catch {
      return [];
    }
  }
  if (typeof lastUsed === "string" && lastUsed && dirs.includes(lastUsed)) {
    dirs = [lastUsed, ...dirs.filter((dir) => dir !== lastUsed)];
  }
  return dirs
    .map((dir) => ({
      id: dir,
      name: String(propertyOf(propertyOf(infoCache, dir), "name") || dir),
      cookieDbPath: path.join(rootDir, dir, "Cookies"),
    }))
    .filter((profile) => fsImpl.existsSync(profile.cookieDbPath));
}

/**
 * Hand `fn` a private copy of Chrome's cookie database, then clean up.
 *
 * Copying the database plus its WAL/journal keeps `sqlite3` away from Chrome's
 * live file entirely, so sqlite's own recovery runs on the copy: nothing here
 * can checkpoint or otherwise rewrite Chrome's database.
 *
 */
async function withCookieSnapshot<T>(
  { cookieDbPath, fsImpl = fs }: { cookieDbPath: string; fsImpl?: SnapshotFs },
  fn: (snapshotPath: string) => Promise<T>,
): Promise<T> {
  const workDir = await fsImpl.promises.mkdtemp(path.join(os.tmpdir(), "omnigent-chrome-cookie-"));
  try {
    const snapshot = path.join(workDir, "Cookies");
    await fsImpl.promises.copyFile(cookieDbPath, snapshot);
    for (const suffix of ["-wal", "-journal"]) {
      // oxlint-disable-next-line no-await-in-loop -- a missing sidecar is expected, so each copy is its own attempt
      await fsImpl.promises
        .copyFile(`${cookieDbPath}${suffix}`, `${snapshot}${suffix}`)
        .catch(() => {});
    }
    return await fn(snapshot);
  } finally {
    await fsImpl.promises.rm(workDir, { recursive: true, force: true }).catch(() => {});
  }
}

/**
 * Select the rows inside the given scopes' trees: each scope host, its
 * subdomains, and the dot-prefixed domain cookies for any of them. Rows outside
 * those trees are never read, and partitioned cookies are left behind.
 *
 */
async function queryCookieRows({
  snapshot,
  scopes,
  run = execFileAsync,
}: {
  snapshot: string;
  scopes: readonly string[] | null | undefined;
  run?: RunCommand;
}): Promise<CookieRow[]> {
  const treeFilters = (scopes ?? []).map((scope) => {
    const literal = escapeSqlLiteral(scope);
    const ancestors = cookieAncestorDomains(scope).map((domain) => `'${escapeSqlLiteral(domain)}'`);
    const inherited = ancestors.length > 0 ? ` OR host_key IN (${ancestors.join(", ")})` : "";
    return `(host_key = '${literal}' OR host_key LIKE '%.${escapeLike(literal)}' ESCAPE '\\'${inherited})`;
  });
  if (treeFilters.length === 0) return [];
  const where = treeFilters.join(" OR ");
  let lastError: unknown = null;
  for (const columns of COLUMN_SETS) {
    try {
      // No `-readonly`: the snapshot keeps Chrome's WAL header, and a WAL
      // database can't be opened read-only when no `-shm` exists yet.
      // oxlint-disable-next-line no-await-in-loop -- each column set must fail before the next is tried
      const { stdout } = await run(SQLITE3_BINARY, [
        "-json",
        snapshot,
        `SELECT ${columns} FROM cookies WHERE ${where}`,
      ]);
      return parseSqliteJson(stdout)
        .map(normalizeRow)
        .filter((row) => row.partitionKey === "");
    } catch (err) {
      lastError = err;
    }
  }
  // Fixed text: the exec error carries the full command line (temp paths, the
  // queried hosts), which must not reach a toast.
  throw new Error(COOKIE_DB_READ_ERROR, { cause: lastError });
}

/**
 * Drop candidate scopes that Chrome holds no domain cookie for. Chrome refuses
 * to store a domain cookie on a public suffix, so requiring the `.<scope>` row
 * keeps the picker at sites the user actually has cookies for instead of
 * offering a whole suffix such as `co.uk` or `github.io`.
 *
 * @returns candidates that survived, order preserved
 */
async function scopeCandidatesWithCookies({
  snapshot,
  candidates,
  run = execFileAsync,
}: {
  snapshot: string;
  candidates: string[];
  run?: RunCommand;
}): Promise<string[]> {
  const ancestors = candidates.slice(1);
  if (ancestors.length === 0) return candidates;
  const literals = ancestors.map((scope) => `'${escapeSqlLiteral(`.${scope}`)}'`).join(", ");
  const { stdout } = await run(SQLITE3_BINARY, [
    "-json",
    snapshot,
    `SELECT host_key FROM cookies WHERE host_key IN (${literals})`,
  ]);
  const present = new Set(parseSqliteJson(stdout).map((row) => String(row.host_key ?? "")));
  return candidates.filter((scope, index) => index === 0 || present.has(`.${scope}`));
}

/**
 * Read `host`'s own tree plus every parent-domain scope Chrome holds a domain
 * cookie for, in one snapshot. The returned rows belong to the widest available
 * scope, which contains the narrower scopes' trees.
 *
 */
export async function readScopeTrees({
  cookieDbPath,
  host,
  run = execFileAsync,
  fsImpl = fs,
}: {
  cookieDbPath: string;
  host: string;
  run?: RunCommand;
  fsImpl?: SnapshotFs;
}): Promise<{ scopes: string[]; rows: CookieRow[] }> {
  const candidates = cookieScopeCandidates(host);
  try {
    return await withCookieSnapshot({ cookieDbPath, fsImpl }, async (snapshot) => {
      const scopes = await scopeCandidatesWithCookies({ snapshot, candidates, run });
      const rows =
        scopes.length === 0
          ? []
          : await queryCookieRows({ snapshot, scopes: scopes.slice(-1), run });
      return { scopes, rows };
    });
  } catch (err) {
    // Snapshot copies and the ancestor query fail outside `queryCookieRows`, so
    // the fixed text is re-applied here to keep paths out of a toast.
    if (messageOf(err) === COOKIE_DB_READ_ERROR) throw err;
    throw new Error(COOKIE_DB_READ_ERROR, { cause: err });
  }
}

/**
 * Read every row inside the given scopes' trees.
 *
 * @returns normalized rows
 */
export async function readCookieRows({
  cookieDbPath,
  scopes,
  run = execFileAsync,
  fsImpl = fs,
}: {
  cookieDbPath: string;
  scopes: string[];
  run?: RunCommand;
  fsImpl?: SnapshotFs;
}): Promise<CookieRow[]> {
  return await withCookieSnapshot({ cookieDbPath, fsImpl }, async (snapshot) =>
    queryCookieRows({ snapshot, scopes, run }),
  );
}

/**
 * Chrome's "Safe Storage" password from the login Keychain. Shows a system
 * approval dialog the first time — the caller must have a user-initiated import
 * behind this.
 *
 */
export async function readSafeStoragePassword({
  run = execFileAsync,
}: { run?: RunCommand } = {}): Promise<string> {
  let stdout: unknown;
  try {
    ({ stdout } = await run(
      SECURITY_BINARY,
      ["find-generic-password", "-w", "-s", SAFE_STORAGE_SERVICE],
      { timeout: KEYCHAIN_TIMEOUT_MS },
    ));
  } catch (err) {
    if (err && propertyOf(err, "code") === 44) {
      throw new Error(
        "Chrome's cookie key isn't in your login Keychain. Open Google Chrome once, then retry.",
        { cause: err },
      );
    }
    if (err && (propertyOf(err, "killed") || propertyOf(err, "signal"))) {
      throw new Error("Timed out waiting for Keychain access. Retry and approve the prompt.", {
        cause: err,
      });
    }
    throw new Error(
      "Couldn't read Chrome's cookie key from the Keychain. Approve the prompt (or unlock the login Keychain) and retry.",
      { cause: err },
    );
  }
  const password = String(stdout ?? "").replace(/\r?\n$/, "");
  if (!password) {
    throw new Error("Chrome's Keychain item is empty. Open Google Chrome once, then retry.");
  }
  return password;
}

/**
 * How many of `host`'s cookies each installed Chrome profile holds. No Keychain
 * access, so this is safe to call as soon as the dialog opens.
 *
 */
export async function previewSiteCookies({
  host,
  rootDir,
  run = execFileAsync,
  fsImpl = fs,
  nowMs = Date.now(),
}: {
  host: unknown;
  rootDir?: string;
  run?: RunCommand;
  fsImpl?: ChromeFs;
  nowMs?: number;
}): Promise<CookiePreview> {
  let normalized: string;
  try {
    normalized = normalizeHost(host);
  } catch {
    return {
      ok: false,
      host: "",
      scopes: [],
      profiles: [],
      error: "This page's address isn't a site we can import cookies for.",
    };
  }
  const candidates = cookieScopeCandidates(normalized);
  const profiles = await listChromeProfiles({ rootDir, fsImpl });
  const found: CookiePreviewProfile[] = [];
  // Profiles can hold different parent-domain cookies, so the picker offers the
  // union; each profile still reports counts only for the scopes it has.
  const offered = new Set<string>();
  for (const profile of profiles) {
    let scopes: string[];
    let rows: CookieRow[];
    try {
      // oxlint-disable-next-line no-await-in-loop -- one Chrome DB at a time keeps disk reads small
      ({ scopes, rows } = await readScopeTrees({
        cookieDbPath: profile.cookieDbPath,
        host: normalized,
        run,
        fsImpl,
      }));
    } catch {
      continue; // An unreadable/locked profile is simply not offered.
    }
    if (scopes.length === 0) continue;
    for (const scope of scopes) offered.add(scope);
    const scopeCounts = scopes.map((scope) => ({
      host: scope,
      cookieCount: selectCookiesForScope(rows, scope, nowMs).length,
    }));
    const [ownScope] = scopeCounts;
    if (ownScope && scopeCounts.some((entry) => entry.cookieCount > 0)) {
      found.push({
        id: profile.id,
        name: profile.name,
        cookieCount: ownScope.cookieCount,
        scopes: scopeCounts,
      });
    }
  }
  // Candidates are ordered from the page host outward, so filtering keeps that
  // order instead of the profiles' discovery order.
  const scopes = candidates.filter((scope) => offered.has(scope));
  return { ok: true, host: normalized, scopes, profiles: found };
}

export interface CookiePreviewProfile {
  id: string;
  name: string;
  /** Cookies for the page host itself. */
  cookieCount: number;
  scopes: { host: string; cookieCount: number }[];
}

export type CookiePreview =
  | { ok: false; host: ""; scopes: []; profiles: []; error: string }
  | { ok: true; host: string; scopes: string[]; profiles: CookiePreviewProfile[] };

export interface ImportSiteCookiesOptions {
  host: unknown;
  /** Renderer-picked scope: the page host or one of its parent domains. */
  scope?: unknown;
  profileId: unknown;
  cookiesApi: { set: (cookie: ElectronCookie) => Promise<unknown> };
  rootDir?: string;
  run?: RunCommand;
  fsImpl?: ChromeFs;
  readPassword?: (options: { run: RunCommand }) => Promise<string>;
  nowMs?: number;
  beforeWrite?: () => boolean;
}

export type ImportSiteCookiesResult =
  | { ok: false; error: string }
  | {
      ok: true;
      host: string;
      scope: string;
      profileId: string;
      profileName: string;
      found: number;
      imported: number;
      skipped: number;
      failed: number;
    };

/**
 * Decrypt `host`'s cookies for one Chrome profile and write them into the given
 * session, returning counts only (never values).
 */
export async function importSiteCookies({
  host,
  scope,
  profileId,
  cookiesApi,
  rootDir,
  run = execFileAsync,
  fsImpl = fs,
  readPassword = readSafeStoragePassword,
  nowMs = Date.now(),
  beforeWrite = () => true,
}: ImportSiteCookiesOptions): Promise<ImportSiteCookiesResult> {
  const pageHost = normalizeHost(host);
  // The renderer picks a scope, but only from the page's own ancestors: a host
  // it names itself can never widen the import to an unrelated site.
  const target = scope === undefined || scope === null ? pageHost : normalizeHost(scope);
  if (!cookieScopeCandidates(pageHost).includes(target)) {
    return { ok: false, error: "That site scope isn't available for this tab." };
  }
  const profiles = await listChromeProfiles({ rootDir, fsImpl });
  const profile = profiles.find((candidate) => candidate.id === profileId);
  if (!profile) return { ok: false, error: "That Chrome profile wasn't found on this machine." };
  const { scopes, rows } = await readScopeTrees({
    cookieDbPath: profile.cookieDbPath,
    host: pageHost,
    run,
    fsImpl,
  });
  if (!scopes.includes(target)) {
    return { ok: false, error: "That site scope isn't available for this tab." };
  }
  let key: Buffer;
  try {
    key = deriveMacKey(await readPassword({ run }));
  } catch (err) {
    return { ok: false, error: messageOf(err) };
  }
  // Re-checked after the Keychain wait, before anything is written: the tab may
  // have navigated while the system prompt was up.
  if (!beforeWrite()) {
    return {
      ok: false,
      error: "This tab is no longer on that site. Reopen the import dialog to retry.",
    };
  }
  const plan = buildCookiePlan({ rows, scope: target, key, nowMs });
  let imported = 0;
  let failed = 0;
  for (const cookie of plan.cookies) {
    try {
      // oxlint-disable-next-line no-await-in-loop -- one failure must not abort the rest
      await cookiesApi.set(cookie);
      imported += 1;
    } catch {
      failed += 1;
    }
  }
  return {
    ok: true,
    host: pageHost,
    scope: target,
    profileId: profile.id,
    profileName: profile.name,
    found: plan.selectedCount,
    imported,
    skipped: plan.skippedCount,
    failed,
  };
}
