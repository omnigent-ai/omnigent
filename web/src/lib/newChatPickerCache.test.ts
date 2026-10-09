import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { writeLastAgentId } from "./agentPreferences";
import { writeLastHarness } from "./harnessPreferences";
import { getOmnigentServerIdentity } from "./host";
import { writeLastHostChoice, writeLastSandboxProvider } from "./hostPreferences";
import { getCurrentUserId } from "./identity";
import { writeHarnessOption } from "./modePreferences";
import {
  getNewChatPickerCacheKey,
  readNewChatPermissionCache,
  readNewChatPickerCache,
  readNewChatPickerOptionsCache,
  readNewChatWorkspaceCache,
  writeNewChatPermissionCache,
  writeNewChatPickerCache,
  writeNewChatPickerOptionsCache,
  writeNewChatWorkspaceCache,
  type NewChatPickerPreview,
  type NewChatPickerOptions,
} from "./newChatPickerCache";

vi.mock("./host", () => ({ getOmnigentServerIdentity: vi.fn() }));
vi.mock("./identity", () => ({ getCurrentUserId: vi.fn() }));

const NOW = new Date("2026-09-13T12:00:00Z").getTime();
const DAY_MS = 24 * 60 * 60 * 1000;
const preview: NewChatPickerPreview = {
  agent: { name: "claude-native-ui", harness: "claude-native" },
  label: "Claude Code, Model Fable 5.1, Effort Max",
  model: "Fable 5.1",
  effort: "Max",
  smartRouting: false,
};
const menuAgent = {
  ...preview.agent,
  id: "a1",
  display_name: "Claude Code",
  description: null,
  skills: [],
};
const options: NewChatPickerOptions = {
  agent: menuAgent,
  agents: [menuAgent],
  hostId: "host_1",
  sandboxSelected: false,
  model: "fable",
  models: {
    claude: [
      {
        id: "fable",
        model: "provider/model-id",
        displayName: "provider/model-id",
        isDefault: true,
      },
    ],
    codex: [
      {
        id: "coding-model",
        displayName: "Team coding model",
        supportedReasoningEfforts: [{ reasoningEffort: "high" }],
      },
    ],
    pi: [],
  },
};
let key: string;

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-a");
  vi.mocked(getCurrentUserId).mockReturnValue("user-a@example.test");
  key = getNewChatPickerCacheKey("")!;
  writeLastAgentId("a1");
  writeLastHostChoice("host_1");
  writeLastSandboxProvider(null);
  writeHarnessOption("claude-native", { model: "fable", effort: "max", routing: "off" });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  localStorage.clear();
});

describe("newChatPickerCache", () => {
  it("caches host-bound menu choices and catalog metadata verbatim", () => {
    writeNewChatPickerOptionsCache(key, options);
    expect(readNewChatPickerOptionsCache(key)).toEqual(options);
  });

  it("preserves missing catalogs separately from successfully fetched empty catalogs", () => {
    const partialOptions = {
      ...options,
      models: { claude: undefined, codex: [], pi: options.models.pi },
    };
    writeNewChatPickerOptionsCache(key, partialOptions);
    expect(readNewChatPickerOptionsCache(key)).toEqual(partialOptions);
  });

  it("treats legacy empty catalogs as unknown while retaining populated catalogs", () => {
    writeNewChatPickerOptionsCache(key, options);
    const record = JSON.parse(localStorage.getItem(`${key}:options`)!);
    delete record.preview.catalogVersion;
    localStorage.setItem(`${key}:options`, JSON.stringify(record));

    const migrated = { ...options, models: { ...options.models, pi: undefined } };
    expect(readNewChatPickerOptionsCache(key)).toEqual(migrated);
    writeNewChatPickerOptionsCache(key, readNewChatPickerOptionsCache(key));
    expect(readNewChatPickerOptionsCache(key)).toEqual(migrated);
  });

  it("isolates and expires cached menu choices just like their labels", () => {
    writeNewChatPickerOptionsCache(key, options);
    expect(readNewChatPickerOptionsCache(getNewChatPickerCacheKey("another-project"))).toBeNull();
    expect(readNewChatPickerOptionsCache(getNewChatPickerCacheKey("", "another-user"))).toBeNull();
    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-b");
    expect(readNewChatPickerOptionsCache(getNewChatPickerCacheKey(""))).toBeNull();
    vi.setSystemTime(NOW + DAY_MS + 1);
    expect(readNewChatPickerOptionsCache(key)).toBeNull();
  });

  it("invalidates cached choices when their target or selection changes", () => {
    writeNewChatPickerOptionsCache(key, options);
    writeLastHostChoice("another-host");
    expect(readNewChatPickerOptionsCache(key)).toBeNull();
    writeLastHostChoice("host_1");
    writeHarnessOption("claude-native", { model: "another-model" });
    expect(readNewChatPickerOptionsCache(key)).toBeNull();
  });

  it.each([
    { ...options, agents: null },
    { ...options, hostId: 42 },
    { ...options, catalogVersion: 2 },
    { ...options, models: { ...options.models, claude: [{ id: "fable", displayName: false }] } },
    {
      ...options,
      models: {
        ...options.models,
        codex: [{ id: "coding-model", supportedReasoningEfforts: ["high"] }],
      },
    },
  ])("ignores malformed menu data", (invalidOptions) => {
    writeNewChatPickerOptionsCache(key, options);
    const record = JSON.parse(localStorage.getItem(`${key}:options`)!);
    localStorage.setItem(`${key}:options`, JSON.stringify({ ...record, preview: invalidOptions }));
    expect(readNewChatPickerOptionsCache(key)).toBeNull();
  });

  it("clears menu choices without clearing the display preview", () => {
    writeNewChatPickerCache(key, preview);
    writeNewChatPickerOptionsCache(key, options);
    writeNewChatPickerOptionsCache(key, null);
    expect(readNewChatPickerOptionsCache(key)).toBeNull();
    expect(readNewChatPickerCache(key)).toEqual(preview);
  });

  it("persists the display preview without adding launch or catalog data", () => {
    writeNewChatPickerCache(key, preview);

    expect(readNewChatPickerCache(key)).toEqual(preview);
    expect(JSON.parse(localStorage.getItem(key)!)).toEqual({
      preview,
      preferences: expect.any(String),
      savedAt: NOW,
    });
  });

  it("reads the persisted preview after a fresh module load", async () => {
    writeNewChatPickerCache(key, preview);
    vi.resetModules();
    const reloaded = await import("./newChatPickerCache");

    expect(reloaded.readNewChatPickerCache(key)).toEqual(preview);
  });

  it("round-trips the Smart Routing display flag", () => {
    const routed = { ...preview, model: "Smart Routing", effort: "", smartRouting: true };
    writeNewChatPickerCache(key, routed);
    expect(readNewChatPickerCache(key)).toEqual(routed);
  });

  it("returns no preview for an unwritten key", () => {
    expect(readNewChatPickerCache(key)).toBeNull();
  });

  it("expires previews after 24 hours", () => {
    writeNewChatPickerCache(key, preview);
    vi.setSystemTime(NOW + DAY_MS - 1);
    expect(readNewChatPickerCache(key)).toEqual(preview);

    vi.setSystemTime(NOW + DAY_MS + 1);
    expect(readNewChatPickerCache(key)).toBeNull();
  });

  it("rejects a timestamp from the future", () => {
    writeNewChatPickerCache(key, preview);
    vi.setSystemTime(NOW - 1);
    expect(readNewChatPickerCache(key)).toBeNull();
  });

  it("isolates records by server, user, and project", () => {
    writeNewChatPickerCache(key, preview);
    const projectKey = getNewChatPickerCacheKey("Alpha")!;
    expect(projectKey).not.toBe(key);
    expect(readNewChatPickerCache(projectKey)).toBeNull();
    const projectPreview = { ...preview, model: "Project model" };
    writeNewChatPickerCache(projectKey, projectPreview);

    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-b");
    const otherServerKey = getNewChatPickerCacheKey("");
    expect(otherServerKey).not.toBe(key);
    expect(readNewChatPickerCache(otherServerKey)).toBeNull();

    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-a");
    vi.mocked(getCurrentUserId).mockReturnValue("user-b@example.test");
    const otherUserKey = getNewChatPickerCacheKey("");
    expect(otherUserKey).not.toBe(key);
    expect(readNewChatPickerCache(otherUserKey)).toBeNull();

    vi.mocked(getCurrentUserId).mockReturnValue("user-a@example.test");
    expect(getNewChatPickerCacheKey("")).toBe(key);
    expect(readNewChatPickerCache(key)).toEqual(preview);
    expect(readNewChatPickerCache(projectKey)).toEqual(projectPreview);
  });

  it("keeps delimiter-containing scope values distinct", () => {
    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server:user");
    vi.mocked(getCurrentUserId).mockReturnValue("person");
    const first = getNewChatPickerCacheKey("Alpha:Beta");
    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server");
    vi.mocked(getCurrentUserId).mockReturnValue("user:person");
    expect(getNewChatPickerCacheKey("Alpha:Beta")).not.toBe(first);
  });

  it("uses an explicitly resolved user before the synchronous identity getter is ready", () => {
    writeNewChatPickerCache(key, preview);
    vi.mocked(getCurrentUserId).mockReturnValue(null);

    expect(getNewChatPickerCacheKey("")).toBeNull();
    const resolvedKey = getNewChatPickerCacheKey("", "user-a@example.test");
    expect(resolvedKey).toBe(key);
    expect(readNewChatPickerCache(resolvedKey)).toEqual(preview);
  });

  it("does not substitute the current user for an explicitly unknown boot identity", () => {
    writeNewChatPickerCache(key, preview);

    expect(getNewChatPickerCacheKey("")).toBe(key);
    expect(getNewChatPickerCacheKey("", null)).toBeNull();
    expect(readNewChatPickerCache(getNewChatPickerCacheKey("", null))).toBeNull();
  });

  it.each(["server", "user", "project"] as const)(
    "disables persistence when the %s scope is unavailable",
    (scope) => {
      if (scope === "server") vi.mocked(getOmnigentServerIdentity).mockReturnValue(null);
      if (scope === "user") vi.mocked(getCurrentUserId).mockReturnValue(null);
      const unavailableKey = getNewChatPickerCacheKey(scope === "project" ? undefined : "");
      expect(unavailableKey).toBeNull();

      const setItem = vi.spyOn(Storage.prototype, "setItem");
      const removeItem = vi.spyOn(Storage.prototype, "removeItem");
      writeNewChatPickerCache(unavailableKey, preview);
      writeNewChatPickerCache(unavailableKey, null);
      expect(readNewChatPickerCache(unavailableKey)).toBeNull();
      expect(setItem).not.toHaveBeenCalled();
      expect(removeItem).not.toHaveBeenCalled();
    },
  );

  it.each([
    { preference: "agent", change: () => writeLastAgentId("another-agent") },
    { preference: "harness", change: () => writeLastHarness("a1", "auto-native") },
    { preference: "host", change: () => writeLastHostChoice("another-host") },
    { preference: "provider", change: () => writeLastSandboxProvider("modal") },
    { preference: "model", change: () => writeHarnessOption("claude-native", { model: "opus" }) },
    { preference: "effort", change: () => writeHarnessOption("claude-native", { effort: "low" }) },
    { preference: "mode", change: () => writeHarnessOption("claude-native", { mode: "plan" }) },
    { preference: "routing", change: () => writeHarnessOption("claude-native", { routing: "on" }) },
  ])("invalidates a preview after the saved $preference changes", ({ change }) => {
    writeNewChatPickerCache(key, preview);
    change();
    expect(readNewChatPickerCache(key)).toBeNull();
  });

  it("invalidates the preview when a saved preference is removed", () => {
    writeNewChatPickerCache(key, preview);
    localStorage.removeItem("omnigent:last-host-choice");
    expect(readNewChatPickerCache(key)).toBeNull();
  });

  it("uses the native agent's harness when its cached wire harness is absent", () => {
    const legacyPreview = { ...preview, agent: { ...preview.agent, harness: null } };
    writeNewChatPickerCache(key, legacyPreview);
    expect(readNewChatPickerCache(key)).toEqual(legacyPreview);
    writeHarnessOption("claude-native", { model: "opus" });
    expect(readNewChatPickerCache(key)).toBeNull();
  });

  it("keeps a preview when another harness's preferences change", () => {
    writeNewChatPickerCache(key, preview);
    writeHarnessOption("codex-native", { model: "codex-model", effort: "low" });
    expect(readNewChatPickerCache(key)).toEqual(preview);
  });

  it("ignores object-key ordering in otherwise identical harness preferences", () => {
    writeNewChatPickerCache(key, preview);
    localStorage.setItem(
      "omnigent:last-mode-by-harness",
      JSON.stringify({ "claude-native": { routing: "off", effort: "max", model: "fable" } }),
    );
    expect(readNewChatPickerCache(key)).toEqual(preview);
  });

  it.each(["{", "null", "[]", "42", "{}", '"not a record"'])(
    "ignores corrupt or invalid stored JSON: %s",
    (raw) => {
      localStorage.setItem(key, raw);
      expect(readNewChatPickerCache(key)).toBeNull();
    },
  );

  it.each([
    { field: "agent", invalidPreview: { ...preview, agent: null } },
    { field: "agent name", invalidPreview: { ...preview, agent: { ...preview.agent, name: 1 } } },
    { field: "harness", invalidPreview: { ...preview, agent: { ...preview.agent, harness: 1 } } },
    { field: "label", invalidPreview: { ...preview, label: null } },
    { field: "model", invalidPreview: { ...preview, model: [] } },
    { field: "effort", invalidPreview: { ...preview, effort: false } },
    { field: "routing", invalidPreview: { ...preview, smartRouting: "on" } },
  ])("ignores a record with an invalid $field", ({ invalidPreview }) => {
    writeNewChatPickerCache(key, preview);
    const record = JSON.parse(localStorage.getItem(key)!) as Record<string, unknown>;
    localStorage.setItem(key, JSON.stringify({ ...record, preview: invalidPreview }));
    expect(readNewChatPickerCache(key)).toBeNull();
  });

  it.each([{ savedAt: "yesterday" }, { savedAt: null }, { preferences: [] }])(
    "ignores invalid cache metadata: %j",
    (metadata) => {
      writeNewChatPickerCache(key, preview);
      const record = JSON.parse(localStorage.getItem(key)!) as Record<string, unknown>;
      localStorage.setItem(key, JSON.stringify({ ...record, ...metadata }));
      expect(readNewChatPickerCache(key)).toBeNull();
    },
  );

  it("removes only the specified preview when cleared", () => {
    const projectKey = getNewChatPickerCacheKey("Alpha")!;
    writeNewChatPickerCache(key, preview);
    writeNewChatPickerCache(projectKey, preview);
    writeNewChatPickerCache(key, null);

    expect(localStorage.getItem(key)).toBeNull();
    expect(readNewChatPickerCache(key)).toBeNull();
    expect(readNewChatPickerCache(projectKey)).toEqual(preview);
  });

  it("falls back to no preview when storage reads are denied", () => {
    writeNewChatPickerCache(key, preview);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("Storage unavailable");
    });
    expect(readNewChatPickerCache(key)).toBeNull();
  });

  it.each(["setItem", "removeItem"] as const)("does not throw when storage %s fails", (method) => {
    vi.spyOn(Storage.prototype, method).mockImplementation(() => {
      throw new Error("Storage unavailable");
    });
    expect(() => writeNewChatPickerCache(key, method === "setItem" ? preview : null)).not.toThrow();
  });

  it("handles an inaccessible localStorage property", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new Error("Storage unavailable");
    });
    expect(readNewChatPickerCache(key)).toBeNull();
    expect(() => writeNewChatPickerCache(key, preview)).not.toThrow();
    expect(() => writeNewChatPickerCache(key, null)).not.toThrow();
  });

  it("does not access browser storage without a window", () => {
    vi.stubGlobal("window", undefined);
    expect(readNewChatPickerCache(key)).toBeNull();
    expect(() => writeNewChatPickerCache(key, preview)).not.toThrow();
  });
});

describe("directory and permission previews", () => {
  const permission = { agent: preview.agent, row: { label: "Permission mode", value: "Plan" } };
  const workspace = {
    hostId: "host_1",
    workspace: "/work/repo",
    repositoryLabel: "repo",
    branchLabel: "Choose",
    branchDescription: "Create or select a worktree from main repository branch: main",
  };

  it("stores each control independently so resolving one cannot erase another", () => {
    writeNewChatPickerCache(key, preview);
    writeNewChatPermissionCache(key, permission);
    writeNewChatWorkspaceCache(key, workspace);
    expect(readNewChatPickerCache(key)).toEqual(preview);
    expect(readNewChatPermissionCache(key)).toEqual(permission);
    expect(readNewChatWorkspaceCache(key)).toEqual(workspace);

    writeNewChatPickerCache(key, null);
    expect(readNewChatPermissionCache(key)).toEqual(permission);
    expect(readNewChatWorkspaceCache(key)).toEqual(workspace);
  });

  it("remembers when a harness has no permission control", () => {
    const noPermissions = { ...permission, row: null };
    writeNewChatPermissionCache(key, noPermissions);
    expect(readNewChatPermissionCache(key)).toEqual(noPermissions);
  });

  it("invalidates permissions, but not the directory, after a mode change", () => {
    writeNewChatPermissionCache(key, permission);
    writeNewChatWorkspaceCache(key, workspace);
    writeHarnessOption("claude-native", { mode: "acceptEdits" });
    expect(readNewChatPermissionCache(key)).toBeNull();
    expect(readNewChatWorkspaceCache(key)).toEqual(workspace);
  });

  it("invalidates a directory after another tab chooses a different recent folder", () => {
    localStorage.setItem("omnigent:recent-workspaces", JSON.stringify({ host_1: ["/work/repo"] }));
    writeNewChatWorkspaceCache(key, workspace);
    localStorage.setItem("omnigent:recent-workspaces", JSON.stringify({ host_1: ["/work/other"] }));
    expect(readNewChatWorkspaceCache(key)).toBeNull();
  });

  it("invalidates both previews after a host change", () => {
    writeNewChatPermissionCache(key, permission);
    writeNewChatWorkspaceCache(key, workspace);
    writeLastHostChoice("another-host");
    expect(readNewChatPermissionCache(key)).toBeNull();
    expect(readNewChatWorkspaceCache(key)).toBeNull();
  });

  it("expires and isolates both previews with the same account/server/project scope", () => {
    writeNewChatPermissionCache(key, permission);
    writeNewChatWorkspaceCache(key, workspace);
    for (const otherKey of [
      getNewChatPickerCacheKey("other-project"),
      getNewChatPickerCacheKey("", "other-user"),
      null,
    ]) {
      expect(readNewChatPermissionCache(otherKey)).toBeNull();
      expect(readNewChatWorkspaceCache(otherKey)).toBeNull();
    }
    vi.mocked(getOmnigentServerIdentity).mockReturnValue("other-server");
    expect(readNewChatPermissionCache(getNewChatPickerCacheKey(""))).toBeNull();
    expect(readNewChatWorkspaceCache(getNewChatPickerCacheKey(""))).toBeNull();
    vi.setSystemTime(NOW + DAY_MS + 1);
    expect(readNewChatPermissionCache(key)).toBeNull();
    expect(readNewChatWorkspaceCache(key)).toBeNull();
  });

  it("clears only the requested preview and safely ignores malformed records", () => {
    writeNewChatPermissionCache(key, permission);
    writeNewChatWorkspaceCache(key, workspace);
    writeNewChatPermissionCache(key, null);
    expect(readNewChatPermissionCache(key)).toBeNull();
    expect(readNewChatWorkspaceCache(key)).toEqual(workspace);
    localStorage.setItem(`${key}:workspace`, JSON.stringify({ preview: { workspace: 42 } }));
    localStorage.setItem(`${key}:permissions`, "{");
    expect(readNewChatPermissionCache(key)).toBeNull();
    expect(readNewChatWorkspaceCache(key)).toBeNull();
  });

  it("falls back safely when storage is unavailable", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new Error("unavailable");
    });
    expect(() => writeNewChatPermissionCache(key, permission)).not.toThrow();
    expect(() => writeNewChatWorkspaceCache(key, workspace)).not.toThrow();
    expect(readNewChatPermissionCache(key)).toBeNull();
    expect(readNewChatWorkspaceCache(key)).toBeNull();
  });
});
