import { describe, expect, it, vi } from "vitest";
import {
  createDbcertLoginWatcher,
  extractDbcertLoginUrl,
  isDbcertLoginUrl,
  openDbcertLogin,
} from "./dbcertLogin";

/**
 * A faithful authorize URL: the shape an OAuth2 client builds from dbcert's
 * config, PKCE params and all.
 */
function authorizeUrl({
  host = "databricks.okta.com",
  port = 4280,
  path = "/oauth2/v1/authorize",
  callbackPath = "/v1/dbcert/callback",
  state = "hKFo2SB1c2Vy",
}: {
  host?: string;
  port?: number;
  path?: string;
  callbackPath?: string;
  state?: string;
} = {}): string {
  const redirect = encodeURIComponent(`http://localhost:${port}${callbackPath}`);
  return (
    `https://${host}${path}?client_id=0oa1b2c3d4e5f6g7h8i9` +
    `&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256` +
    `&nonce=nQ3pAyEtM1&redirect_uri=${redirect}` +
    `&response_type=code&scope=openid+profile+groups&state=${state}`
  );
}

/**
 * dbcert's prompt as it reaches a pane: its logger's timestamp prefix, the URL
 * indented on its own line, and the OSC 8 hyperlink wrapper it adds whenever
 * stderr is a terminal (so the URL appears twice).
 */
function dbcertPrompt(url: string): string {
  return (
    "Running dbcert to obtain a new certificate, please follow its instructions.\r\n" +
    "2026/09/10 11:02:41 If the browser does not open automatically, " +
    "please open the following URL:\r\n\r\n\t" +
    `\x1b]8;;${url}\x1b\\${url}\x1b]8;;\x1b\\\r\n\r\n`
  );
}

describe("isDbcertLoginUrl", () => {
  it("accepts an authorize URL for each dbcert Okta environment", () => {
    for (const host of [
      "databricks.okta.com",
      "regulated-databricks.okta-gov.com",
      "corpengdatabricks.oktapreview.com",
    ]) {
      expect(isDbcertLoginUrl(authorizeUrl({ host }))).toBe(true);
    }
  });

  it("accepts any loopback callback port", () => {
    // dbcert moves its listener off the default port in some environments, so
    // the port must not be part of the match.
    expect(isDbcertLoginUrl(authorizeUrl({ port: 4281 }))).toBe(true);
  });

  it("rejects an Okta authorize URL redirecting somewhere other than dbcert", () => {
    // Other tools' OAuth also lands on an Okta authorize endpoint; the callback
    // path is what tells them apart.
    expect(isDbcertLoginUrl(authorizeUrl({ callbackPath: "/callback", port: 8020 }))).toBe(false);
  });

  it("rejects a non-allowlisted host wearing the right query", () => {
    expect(isDbcertLoginUrl(authorizeUrl({ host: "evil.example.com" }))).toBe(false);
    expect(isDbcertLoginUrl(authorizeUrl({ host: "databricks.okta.com.evil.example" }))).toBe(
      false,
    );
  });

  it("rejects a different path on an allowlisted host", () => {
    expect(isDbcertLoginUrl(authorizeUrl({ path: "/oauth2/v1/token" }))).toBe(false);
  });

  it("rejects http, missing redirect_uri, and unparseable input", () => {
    expect(isDbcertLoginUrl(authorizeUrl().replace("https://", "http://"))).toBe(false);
    expect(isDbcertLoginUrl("https://databricks.okta.com/oauth2/v1/authorize?client_id=x")).toBe(
      false,
    );
    expect(isDbcertLoginUrl("not a url")).toBe(false);
    expect(isDbcertLoginUrl("")).toBe(false);
  });
});

describe("extractDbcertLoginUrl", () => {
  it("pulls the URL out of dbcert's OSC 8 linkified prompt", () => {
    const url = authorizeUrl();
    expect(extractDbcertLoginUrl(dbcertPrompt(url))).toBe(url);
  });

  it("finds the URL when dbcert's stderr was not a terminal (no OSC 8 wrapper)", () => {
    const url = authorizeUrl();
    expect(extractDbcertLoginUrl(`please open the following URL:\r\n\r\n\t${url}\r\n\r\n`)).toBe(
      url,
    );
  });

  it("survives SGR colouring around the URL", () => {
    const url = authorizeUrl();
    expect(extractDbcertLoginUrl(`\x1b[36m${url}\x1b[0m\r\n`)).toBe(url);
  });

  it("returns the last URL when a redraw replays an older one", () => {
    const stale = authorizeUrl({ state: "stale" });
    const live = authorizeUrl({ state: "live" });
    expect(extractDbcertLoginUrl(dbcertPrompt(stale) + dbcertPrompt(live))).toBe(live);
  });

  it("stays quiet for ordinary output and dbcert's other chatter", () => {
    expect(extractDbcertLoginUrl("")).toBeNull();
    expect(extractDbcertLoginUrl("Running dbcert to obtain a new certificate.\r\n")).toBeNull();
    expect(extractDbcertLoginUrl("Successfully obtained new dbcert certificates.\r\n")).toBeNull();
    expect(extractDbcertLoginUrl("https://github.com/omnigent-ai/omnigent/pull/1 \r\n")).toBeNull();
  });

  it("does not commit a URL truncated by a chunk boundary", () => {
    const url = authorizeUrl();
    const cut = url.slice(0, url.indexOf("&response_type"));
    expect(cut).toContain("redirect_uri");
    expect(extractDbcertLoginUrl(`following URL:\r\n\r\n\t${cut}`)).toBeNull();
  });
});

describe("createDbcertLoginWatcher", () => {
  it("reports a login URL once per distinct URL", () => {
    const onLogin = vi.fn();
    const watch = createDbcertLoginWatcher(onLogin);
    const url = authorizeUrl();
    watch(dbcertPrompt(url));
    watch(dbcertPrompt(url));
    expect(onLogin.mock.calls).toEqual([[url]]);
  });

  it("reassembles a URL split across frames", () => {
    const onLogin = vi.fn();
    const watch = createDbcertLoginWatcher(onLogin);
    const url = authorizeUrl();
    const text = dbcertPrompt(url);
    const at = text.indexOf("&nonce");
    watch(text.slice(0, at));
    expect(onLogin).not.toHaveBeenCalled();
    watch(text.slice(at));
    expect(onLogin.mock.calls).toEqual([[url]]);
  });

  it("reports a second, different login", () => {
    const onLogin = vi.fn();
    const watch = createDbcertLoginWatcher(onLogin);
    const first = authorizeUrl({ state: "first" });
    const second = authorizeUrl({ state: "second" });
    watch(dbcertPrompt(first));
    watch(dbcertPrompt(second));
    expect(onLogin.mock.calls).toEqual([[first], [second]]);
  });

  it("ignores empty chunks and plain output", () => {
    const onLogin = vi.fn();
    const watch = createDbcertLoginWatcher(onLogin);
    watch("");
    watch("$ ls -la\r\n");
    expect(onLogin).not.toHaveBeenCalled();
  });
});

describe("openDbcertLogin", () => {
  const url = authorizeUrl();

  it("opens the URL in a new tab with no handle back to this window", () => {
    const open = vi.fn(() => ({}) as Window);
    expect(openDbcertLogin(url, { open } as unknown as Window)).toBe(true);
    expect(open).toHaveBeenCalledWith(url, "_blank", "noopener,noreferrer");
  });

  it("reports a popup-blocked open", () => {
    const open = vi.fn(() => null);
    expect(openDbcertLogin(url, { open } as unknown as Window)).toBe(false);
  });
});
