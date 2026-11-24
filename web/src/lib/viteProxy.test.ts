import { EventEmitter } from "node:events";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { loadConfigFromFile, type ProxyOptions } from "vite";
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

it("re-mints the profile's CLI token before expiry and keeps it through failed refreshes", async () => {
  const bin = mkdtempSync(path.join(tmpdir(), "fake-databricks-"));
  const calls = path.join(bin, "calls");
  const failing = path.join(bin, "failing");
  writeFileSync(
    path.join(bin, "databricks"),
    `#!/bin/sh
echo "$*" >> "$FAKE_DATABRICKS_CALLS"
[ -e "$FAKE_DATABRICKS_FAILING" ] && exit 1
printf '{"access_token":"token-%s","expiry":"2030-01-01T01:00:00.123456789Z"}' $(($(wc -l < "$FAKE_DATABRICKS_CALLS")))
`,
    { mode: 0o755 },
  );
  try {
    vi.stubEnv("PATH", `${bin}${path.delimiter}${process.env.PATH}`);
    vi.stubEnv("FAKE_DATABRICKS_CALLS", calls);
    vi.stubEnv("FAKE_DATABRICKS_FAILING", failing);
    vi.stubEnv("OMNIGENT_URL", origin);
    vi.stubEnv("OMNIGENT_AUTH_TOKEN", "");
    vi.stubEnv("OMNIGENT_DATABRICKS_PROFILE", "dev-profile");
    const loaded = await loadConfigFromFile({ command: "serve", mode: "development" }, configPath);
    const proxy = loaded?.config.server?.proxy?.["/v1"] as ProxyOptions;
    const events = new EventEmitter();
    proxy.configure?.(events as never, proxy);
    const authorization = () => {
      const sent: Record<string, unknown> = {};
      events.emit("proxyReq", {
        path: "/v1/sessions",
        setHeader: (name: string, value: unknown) => (sent[name] = value),
      });
      return sent.Authorization;
    };

    const at = (time: string) => {
      vi.setSystemTime(new Date(`2030-01-01T${time}Z`));
      return authorization();
    };

    vi.useFakeTimers({ toFake: ["Date"] });
    expect(at("00:00:00")).toBe("Bearer token-1");
    expect(at("00:58:00")).toBe("Bearer token-1");
    expect(at("00:59:30")).toBe("Bearer token-2");
    writeFileSync(failing, "");
    // A failed refresh keeps the unexpired token and waits 10s before retrying.
    expect(at("00:59:45")).toBe("Bearer token-2");
    expect(at("00:59:50")).toBe("Bearer token-2");
    expect(at("01:00:01")).toBeUndefined();
    rmSync(failing);
    expect(at("01:00:12")).toBe("Bearer token-5");
    expect(readFileSync(calls, "utf8")).toBe(
      "auth token --profile dev-profile --output json\n".repeat(5),
    );
  } finally {
    vi.useRealTimers();
    vi.unstubAllEnvs();
    rmSync(bin, { recursive: true, force: true });
  }
});
