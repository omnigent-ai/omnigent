import { EventEmitter } from "node:events";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { loadConfigFromFile, type Plugin, type ProxyOptions } from "vite";
import { expect, it, vi } from "vitest";

const origin = "http://localhost:6767";
const headers = { "X-Workspace-Id": "123" };
const orgHeader = { "X-Databricks-Org-Id": "42" };
const configPath = path.resolve(__dirname, "../../vite.config.ts");

it.each([
  { name: "unset", setting: undefined, expected: {} },
  { name: "matching origin", setting: JSON.stringify({ [origin]: headers }), expected: headers },
  {
    name: "other origin",
    setting: JSON.stringify({ "https://example.com": headers }),
    expected: {},
  },
  { name: "org selector", url: `${origin}/?o=42`, setting: undefined, expected: orgHeader },
  {
    name: "org selector with headers",
    url: `${origin}/?o=42`,
    setting: JSON.stringify({ [origin]: headers }),
    expected: { ...orgHeader, ...headers },
  },
  { name: "non-numeric org selector", url: `${origin}/?o=abc`, setting: undefined, expected: null },
  ...[
    "{",
    "null",
    "[]",
    JSON.stringify({ [origin]: null }),
    JSON.stringify({ [origin]: { "X-Id": 123 } }),
    JSON.stringify({ [origin]: { "Bad Name": "123" } }),
    JSON.stringify({ [origin]: { "X-Id": "a\r\nb" } }),
  ].map((setting) => ({ name: "invalid setting", url: origin, setting, expected: null })),
])("configures proxy headers: $name", async ({ url = origin, setting, expected }) => {
  try {
    vi.stubEnv("OMNIGENT_URL", url);
    vi.stubEnv("OMNIGENT_AUTH_TOKEN", "");
    vi.stubEnv("OMNIGENT_PROXY_HEADERS", setting);
    const result = loadConfigFromFile({ command: "serve", mode: "development" }, configPath);
    if (expected === null) {
      await expect(result).rejects.toThrow();
      return;
    }
    const proxies = (await result)?.config.server?.proxy;
    expect(Object.keys(proxies ?? {})).toEqual(["/v1", "/api", "/auth", "/health", "/.well-known"]);
    for (const proxy of Object.values(proxies ?? {})) {
      expect(typeof proxy === "object" && proxy.headers).toEqual(expected);
    }
  } finally {
    vi.unstubAllEnvs();
  }
});

const loadServeConfig = async () => {
  const loaded = await loadConfigFromFile({ command: "serve", mode: "development" }, configPath);
  const plugins = (loaded?.config.plugins ?? []).flat() as Plugin[];
  // The startup check waits for the in-flight token refresh, so tests also use it as a join point.
  const startupCheck = plugins.find((plugin) => plugin.name === "dev-proxy-auth")
    ?.config as () => Promise<void>;
  return { proxy: loaded?.config.server?.proxy?.["/v1"] as ProxyOptions, startupCheck };
};

it("refreshes the profile's CLI token in the background and keeps it through failed refreshes", async () => {
  const bin = mkdtempSync(path.join(tmpdir(), "fake-databricks-"));
  const calls = path.join(bin, "calls");
  const failing = path.join(bin, "failing");
  writeFileSync(
    path.join(bin, "databricks"),
    `#!/bin/sh
echo "$*" >> "$FAKE_DATABRICKS_CALLS"
[ -e "$FAKE_DATABRICKS_FAILING" ] && exit 1
printf '{"access_token":"token-%s","expiry":"%s"}' $(($(wc -l < "$FAKE_DATABRICKS_CALLS"))) "$FAKE_DATABRICKS_EXPIRY"
`,
    { mode: 0o755 },
  );
  try {
    vi.stubEnv("PATH", `${bin}${path.delimiter}${process.env.PATH}`);
    vi.stubEnv("FAKE_DATABRICKS_CALLS", calls);
    vi.stubEnv("FAKE_DATABRICKS_FAILING", failing);
    vi.stubEnv("FAKE_DATABRICKS_EXPIRY", "2030-01-01T01:00:00.123456789Z");
    vi.stubEnv("OMNIGENT_URL", origin);
    vi.stubEnv("OMNIGENT_AUTH_TOKEN", "");
    vi.stubEnv("OMNIGENT_DATABRICKS_PROFILE", "dev-profile");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { proxy, startupCheck } = await loadServeConfig();
    const events = new EventEmitter();
    const authorization = () => {
      const sent: Record<string, unknown> = {};
      events.emit("proxyReq", {
        path: "/v1/sessions",
        setHeader: (name: string, value: unknown) => (sent[name] = value),
      });
      return sent.Authorization;
    };
    const at = async (time: string, { awaitRefresh = false } = {}) => {
      vi.setSystemTime(new Date(`2030-01-01T${time}Z`));
      const sent = authorization();
      if (awaitRefresh) await startupCheck();
      return sent;
    };
    const refresh = { awaitRefresh: true };

    // Jumping the clock with setSystemTime never fires the scheduled refresh; only advancing does.
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    vi.setSystemTime(new Date("2030-01-01T00:00:00Z"));
    await startupCheck();
    proxy.configure?.(events as never, proxy);
    expect(await at("00:00:00")).toBe("Bearer token-1");
    expect(await at("00:58:00")).toBe("Bearer token-1");
    // A minute before expiry the request keeps the current token while a new one is minted.
    expect(await at("00:59:30", refresh)).toBe("Bearer token-1");
    expect(await at("00:59:31")).toBe("Bearer token-2");
    writeFileSync(failing, "");
    // A failed refresh keeps the unexpired token and waits 10s before retrying.
    expect(await at("00:59:45", refresh)).toBe("Bearer token-2");
    expect(await at("00:59:50")).toBe("Bearer token-2");
    expect(await at("00:59:58", refresh)).toBe("Bearer token-2");
    expect(warn).toHaveBeenCalledTimes(1);
    // Expiry falls inside that wait: nothing is sent, and the CLI is not called early.
    expect(await at("01:00:01")).toBeUndefined();
    rmSync(failing);
    vi.stubEnv("FAKE_DATABRICKS_EXPIRY", "2030-01-01T02:00:00Z");
    expect(await at("01:00:08", refresh)).toBeUndefined();
    expect(await at("01:00:09")).toBe("Bearer token-5");
    // While idle, the token is renewed on schedule, so the next request after expiry has one.
    vi.stubEnv("FAKE_DATABRICKS_EXPIRY", "2030-01-01T03:00:00Z");
    vi.advanceTimersByTime(59 * 60_000);
    // Poll before the old refresh deadline, where requests never refresh: only the timer can have.
    vi.setSystemTime(new Date("2030-01-01T01:30:00Z"));
    await vi.waitFor(() => expect(authorization()).toBe("Bearer token-6"));
    expect(await at("02:00:10")).toBe("Bearer token-6");
    // A static token wins over the profile and never calls the CLI.
    vi.stubEnv("OMNIGENT_AUTH_TOKEN", "static-token");
    expect(await at("02:59:30")).toBe("Bearer static-token");
    expect(readFileSync(calls, "utf8")).toBe(
      "auth token --profile dev-profile --output json\n".repeat(6),
    );
  } finally {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
    rmSync(bin, { recursive: true, force: true });
  }
});

it("never mints a Databricks token for a plain-http remote server", async () => {
  try {
    vi.stubEnv("OMNIGENT_URL", "http://lan.example.com:6767");
    vi.stubEnv("OMNIGENT_AUTH_TOKEN", "");
    vi.stubEnv("OMNIGENT_DATABRICKS_PROFILE", "dev-profile");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(console, "error").mockImplementation(() => {});
    const { startupCheck } = await loadServeConfig();
    await expect(startupCheck()).rejects.toThrow();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("over plain http"));
  } finally {
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
  }
});
