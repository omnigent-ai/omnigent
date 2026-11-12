import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { getOmnigentServerIdentity } from "./host";
import { getCurrentUserId } from "./identity";
import {
  modelCatalogCacheKey,
  readModelCatalogCache,
  SANDBOX_SESSION_CATALOG_MAX_AGE_MS,
  sandboxSessionCatalogKey,
  writeModelCatalogCache,
} from "./modelCatalogCache";

vi.mock("./host", () => ({ getOmnigentServerIdentity: vi.fn() }));
vi.mock("./identity", () => ({ getCurrentUserId: vi.fn() }));
const models = [
  {
    id: "model-a",
    displayName: "Model A",
    isDefault: true,
    supportedReasoningEfforts: [{ reasoningEffort: "high" }],
  },
];
const key = () => modelCatalogCacheKey("sandbox", "company", "codex-native", "agent-a");

beforeEach(() => {
  localStorage.clear();
  vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-a");
  vi.mocked(getCurrentUserId).mockReturnValue("alice");
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it("retains model metadata and replaces stale choices with an authoritative empty catalog", () => {
  writeModelCatalogCache(key(), models, true);
  expect(readModelCatalogCache(key())).toMatchObject({ models, configured: true });
  writeModelCatalogCache(key(), [], false);
  expect(readModelCatalogCache(key())).toMatchObject({ models: [], configured: false });
});

it("does not restore Fusion configurations as selectable model IDs", () => {
  writeModelCatalogCache(
    key(),
    [...models, { id: "fusion", fusion: { combos: [], default: "" } }],
    false,
  );
  expect(readModelCatalogCache(key())?.models).toEqual(models);
});

it.each(["server", "user", "target", "kind", "harness", "agent"])(
  "isolates catalogs by %s",
  (dimension) => {
    writeModelCatalogCache(key(), models, true);
    if (dimension === "server") vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-b");
    if (dimension === "user") vi.mocked(getCurrentUserId).mockReturnValue("bob");
    const other = modelCatalogCacheKey(
      dimension === "kind" ? "host" : "sandbox",
      dimension === "target" ? "other-provider" : "company",
      dimension === "harness" ? "claude-native" : "codex-native",
      dimension === "agent" ? "agent-b" : "agent-a",
    );
    expect(readModelCatalogCache(other)).toBeNull();
  },
);

it.each([
  null,
  "corrupt",
  JSON.stringify({ models: [{ id: 12 }], configured: false, savedAt: Date.now() }),
])("ignores invalid storage %s", (value) => {
  if (value !== null) localStorage.setItem(key()!, value);
  expect(readModelCatalogCache(key())).toBeNull();
});

it("expires catalogs after a day and rejects future timestamps", () => {
  vi.useFakeTimers();
  const now = Date.now();
  writeModelCatalogCache(key(), models, true);
  vi.setSystemTime(now + 24 * 60 * 60 * 1000 + 1);
  expect(readModelCatalogCache(key())).toBeNull();
  vi.setSystemTime(now - 1);
  expect(readModelCatalogCache(key())).toBeNull();
});

it("preserves the fetch time when an inactive query supplies old data", () => {
  vi.useFakeTimers();
  const fetchedAt = Date.now();
  vi.setSystemTime(fetchedAt + 23 * 60 * 60 * 1000);
  writeModelCatalogCache(key(), models, false, fetchedAt);
  expect(readModelCatalogCache(key())?.models).toEqual(models);
  vi.setSystemTime(fetchedAt + 25 * 60 * 60 * 1000);
  expect(readModelCatalogCache(key())).toBeNull();
});

it("does not persist catalogs with an unresolved server or user", () => {
  vi.mocked(getCurrentUserId).mockReturnValue(null);
  expect(key()).toBeNull();
  writeModelCatalogCache(key(), models, true);
  vi.mocked(getCurrentUserId).mockReturnValue("alice");
  vi.mocked(getOmnigentServerIdentity).mockReturnValue(null);
  expect(key()).toBeNull();
  expect(localStorage.length).toBe(0);
});

it("tolerates unavailable browser storage", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  expect(() => writeModelCatalogCache(key(), models, true)).not.toThrow();
  expect(readModelCatalogCache(key())).toBeNull();
});

it("reads a runner-reported catalog that sends null for unset fields", () => {
  const sessionKey = sandboxSessionCatalogKey("company", "claude-native", "ag_1");
  window.localStorage.setItem(
    sessionKey!,
    JSON.stringify({
      models: [
        {
          id: "system.ai.claude-sonnet-5-5",
          model: "system.ai.claude-sonnet-5-5",
          displayName: "Claude Sonnet 5.5",
          defaultReasoningEffort: null,
          supportedReasoningEfforts: [],
          isDefault: null,
          source: { kind: "databricks", label: "Workspace", name: "DEFAULT" },
        },
      ],
      configured: false,
      savedAt: Date.now(),
    }),
  );
  const saved = readModelCatalogCache(sessionKey, SANDBOX_SESSION_CATALOG_MAX_AGE_MS);
  expect(saved?.models).toEqual([
    {
      id: "system.ai.claude-sonnet-5-5",
      model: "system.ai.claude-sonnet-5-5",
      displayName: "Claude Sonnet 5.5",
      supportedReasoningEfforts: [],
      source: { kind: "databricks", label: "Workspace", name: "DEFAULT" },
    },
  ]);
});

it("keeps session catalogs apart from previews and other agents, for a week", () => {
  vi.useFakeTimers();
  const now = Date.now();
  const sessionKey = sandboxSessionCatalogKey("company", "codex-native", "ag_1");
  writeModelCatalogCache(sessionKey, models, false);
  expect(readModelCatalogCache(key())).toBeNull();
  expect(
    readModelCatalogCache(sandboxSessionCatalogKey("company", "claude-native", "ag_1")),
  ).toBeNull();
  expect(
    readModelCatalogCache(sandboxSessionCatalogKey("other", "codex-native", "ag_1")),
  ).toBeNull();
  expect(
    readModelCatalogCache(sandboxSessionCatalogKey("company", "codex-native", "ag_2")),
  ).toBeNull();
  vi.setSystemTime(now + 2 * 24 * 60 * 60 * 1000);
  expect(readModelCatalogCache(sessionKey, SANDBOX_SESSION_CATALOG_MAX_AGE_MS)?.models).toEqual(
    models,
  );
  vi.setSystemTime(now + SANDBOX_SESSION_CATALOG_MAX_AGE_MS + 1);
  expect(readModelCatalogCache(sessionKey, SANDBOX_SESSION_CATALOG_MAX_AGE_MS)).toBeNull();
  expect(sandboxSessionCatalogKey(null, "codex-native", "ag_1")).toBeNull();
  expect(sandboxSessionCatalogKey("company", "codex-native", null)).toBeNull();
  vi.mocked(getCurrentUserId).mockReturnValue("bob");
  expect(sandboxSessionCatalogKey("company", "codex-native", "ag_1")).not.toBe(sessionKey);
  vi.mocked(getCurrentUserId).mockReturnValue("alice");
  vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-b");
  expect(sandboxSessionCatalogKey("company", "codex-native", "ag_1")).not.toBe(sessionKey);
  vi.mocked(getOmnigentServerIdentity).mockReturnValue(null);
  expect(sandboxSessionCatalogKey("company", "codex-native", "ag_1")).toBeNull();
  vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-a");
  vi.mocked(getCurrentUserId).mockReturnValue(null);
  expect(sandboxSessionCatalogKey("company", "codex-native", "ag_1")).toBeNull();
});
