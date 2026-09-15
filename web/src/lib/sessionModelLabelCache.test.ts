import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getOmnigentServerIdentity } from "./host";
import { getCurrentUserId } from "./identity";
import {
  getSessionModelLabelCacheKey,
  readSessionModelLabelCache,
  writeSessionModelLabelCache,
  type SessionModelLabelScope,
} from "./sessionModelLabelCache";

vi.mock("./host", () => ({ getOmnigentServerIdentity: vi.fn() }));
vi.mock("./identity", () => ({ getCurrentUserId: vi.fn() }));

const scope: SessionModelLabelScope = {
  sessionId: "session-a",
  hostId: "host-a",
  agentId: "agent-a",
  harness: "claude-native",
};
const model = "provider/model-a";
const NOW = new Date("2026-09-13T12:00:00Z").getTime();
let key: string;

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-a");
  vi.mocked(getCurrentUserId).mockReturnValue("user-a");
  key = getSessionModelLabelCacheKey(scope, model)!;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  localStorage.clear();
});

describe("session model display-name cache", () => {
  it.each(["Team model (large context)", "provider/model-a"])(
    "persists the exact advertised display name %s",
    (displayName) => {
      writeSessionModelLabelCache(key, displayName);
      expect(readSessionModelLabelCache(key)).toBe(displayName);
      expect(JSON.parse(localStorage.getItem(key)!)).toEqual({ displayName, savedAt: NOW });
    },
  );

  it("survives a fresh module load", async () => {
    writeSessionModelLabelCache(key, "Team model");
    vi.resetModules();
    const reloaded = await import("./sessionModelLabelCache");
    expect(reloaded.readSessionModelLabelCache(key)).toBe("Team model");
  });

  it.each(["sessionId", "hostId", "agentId", "harness"] as const)(
    "isolates names when %s changes",
    (field) => {
      writeSessionModelLabelCache(key, "Team model");
      const otherKey = getSessionModelLabelCacheKey({ ...scope, [field]: "another" }, model);
      expect(readSessionModelLabelCache(otherKey)).toBeNull();
    },
  );

  it("never borrows the label of another reported model, server, or account", () => {
    writeSessionModelLabelCache(key, "Team model");
    expect(
      readSessionModelLabelCache(getSessionModelLabelCacheKey(scope, "other-model")),
    ).toBeNull();
    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-b");
    expect(readSessionModelLabelCache(getSessionModelLabelCacheKey(scope, model))).toBeNull();
    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-a");
    vi.mocked(getCurrentUserId).mockReturnValue("user-b");
    expect(readSessionModelLabelCache(getSessionModelLabelCacheKey(scope, model))).toBeNull();
  });

  it("does not read or write until identity and the reported model are known", () => {
    expect(getSessionModelLabelCacheKey(scope, null)).toBeNull();
    expect(getSessionModelLabelCacheKey({ ...scope, sessionId: null }, model)).toBeNull();
    vi.mocked(getCurrentUserId).mockReturnValue(null);
    expect(getSessionModelLabelCacheKey(scope, model)).toBeNull();
    vi.mocked(getCurrentUserId).mockReturnValue("user-a");
    vi.mocked(getOmnigentServerIdentity).mockReturnValue(null);
    expect(getSessionModelLabelCacheKey(scope, model)).toBeNull();
    writeSessionModelLabelCache(null, "Team model");
    expect(readSessionModelLabelCache(null)).toBeNull();
    expect(localStorage.length).toBe(0);
  });

  it("expires names after one day and rejects future timestamps", () => {
    writeSessionModelLabelCache(key, "Team model");
    vi.setSystemTime(NOW - 1);
    expect(readSessionModelLabelCache(key)).toBeNull();
    vi.setSystemTime(NOW + 24 * 60 * 60 * 1000);
    expect(readSessionModelLabelCache(key)).toBe("Team model");
    vi.setSystemTime(NOW + 24 * 60 * 60 * 1000 + 1);
    expect(readSessionModelLabelCache(key)).toBeNull();
  });

  it.each(["not json", "null", "{}", '{"displayName":42,"savedAt":0}'])(
    "ignores malformed storage: %s",
    (value) => {
      localStorage.setItem(key, value);
      expect(readSessionModelLabelCache(key)).toBeNull();
    },
  );

  it("removes a cached name when live metadata no longer advertises it", () => {
    writeSessionModelLabelCache(key, "Team model");
    writeSessionModelLabelCache(key, null);
    expect(readSessionModelLabelCache(key)).toBeNull();
  });

  it("tolerates unavailable storage", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readSessionModelLabelCache(key)).toBeNull();
    expect(() => writeSessionModelLabelCache(key, "Team model")).not.toThrow();
    expect(() => writeSessionModelLabelCache(key, null)).not.toThrow();
  });
});
