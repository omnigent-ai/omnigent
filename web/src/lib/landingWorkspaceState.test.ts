import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as HostModule from "./host";
import type * as IdentityModule from "./identity";
import { landingStorageKey } from "./landingStorage";
import { toast } from "sonner";

const LANDING_STORAGE_PREFIX = "omnigent:landing-workspace";
const SESSION_STORAGE_KEY = "omnigent:session-workspace-state";
const storageScope = vi.hoisted(() => ({
  server: "server-a",
  user: "user-a" as string | null,
}));

vi.mock("./host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => storageScope.server,
}));
vi.mock("./identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => storageScope.user,
}));
vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));

async function loadLandingWorkspaceState() {
  return import("./landingWorkspaceState");
}

beforeEach(() => {
  localStorage.clear();
  storageScope.server = "server-a";
  storageScope.user = "user-a";
  vi.restoreAllMocks();
  vi.resetModules();
  Reflect.deleteProperty(window, "omnigentDesktop");
  vi.mocked(toast.error).mockReset();
});

describe("landingWorkspaceState", () => {
  it("targets the selected host and folder only while that folder is available", async () => {
    const { landingResourceTarget } = await loadLandingWorkspaceState();
    const selected = {
      hostId: "host/a",
      workspace: "/repo one",
      available: true,
      reason: "",
    };

    expect(landingResourceTarget(selected)).toEqual({
      kind: "host",
      hostId: "host/a",
      workspace: "/repo one",
    });
    expect(
      landingResourceTarget({
        ...selected,
        available: false,
        reason: "The new worktree becomes available after Start.",
      }),
    ).toBeUndefined();
    expect(landingResourceTarget({ ...selected, hostId: null })).toBeUndefined();
  });

  it("persists draft panel state separately from every session workspace", async () => {
    const landing = await loadLandingWorkspaceState();
    const sessions = await import("./sessionWorkspaceState");

    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/workspace",
      available: true,
      reason: "",
    });
    landing.writeLandingWorkspacePanel({
      rightRailTab: "changes",
      openFiles: ["src/draft.ts"],
      selectedFilePath: "src/draft.ts",
      openBrowsers: ["draft-browser"],
    });

    expect(sessions.readSessionWorkspaceState("session-1")).toEqual({});
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();

    sessions.writeSessionWorkspaceState("session-1", {
      openFiles: ["src/session.ts"],
      selectedFilePath: "src/session.ts",
    });

    expect(landing.readLandingWorkspaceState().panel).toMatchObject({
      openFiles: ["src/draft.ts"],
      selectedFilePath: "src/draft.ts",
      openBrowsers: ["draft-browser"],
    });
    expect(
      JSON.parse(localStorage.getItem(landingStorageKey(LANDING_STORAGE_PREFIX)) ?? "null").panel
        .openFiles,
    ).toEqual(["src/draft.ts"]);
  });

  it("restores persisted state after a module reload", async () => {
    const landing = await loadLandingWorkspaceState();
    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/persisted",
      available: true,
      reason: "",
    });
    landing.writeLandingWorkspacePanel({
      rightRailTab: "changes",
      openFiles: ["persisted.ts"],
      selectedFilePath: "persisted.ts",
    });
    const beforeReload = landing.readLandingWorkspaceState();

    vi.resetModules();
    const reloaded = await loadLandingWorkspaceState();

    expect(reloaded.readLandingWorkspaceState()).toEqual({
      ...beforeReload,
      starting: false,
      busy: false,
    });
  });

  it("resets transient starting and busy gates after a module reload", async () => {
    const landing = await loadLandingWorkspaceState();
    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/persisted",
      available: true,
      reason: "",
    });
    landing.setLandingWorkspaceStarting(true);
    landing.setLandingWorkspaceBusy(true);
    expect(landing.readLandingWorkspaceState()).toMatchObject({ starting: true, busy: true });

    vi.resetModules();
    const reloaded = await loadLandingWorkspaceState();

    expect(reloaded.readLandingWorkspaceState()).toMatchObject({
      selection: { hostId: "host-1", workspace: "/persisted", available: true },
      starting: false,
      busy: false,
    });
  });

  it("gives concurrent Starts on one namespace exactly one resource owner", async () => {
    const landing = await loadLandingWorkspaceState();
    const sessions = await import("./sessionWorkspaceState");
    const adopt = vi.fn().mockResolvedValue(undefined);
    const browserAdoptDraft = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserAdoptDraft },
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-a",
      workspace: "/workspace-a",
      available: true,
      reason: "",
    });
    landing.writeLandingWorkspacePanel({
      openTerminals: ["terminal:a"],
      selectedTerminalKey: "terminal:a",
      openBrowsers: ["browser-a"],
      selectedBrowserId: "browser-a",
    });
    const discard = vi.fn().mockResolvedValue(undefined);
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard,
      adopt,
    });

    const first = landing.claimLandingWorkspaceStart();
    const second = landing.claimLandingWorkspaceStart();

    expect(first.ownsResources).toBe(true);
    expect(second.ownsResources).toBe(false);
    expect(first.snapshot.state.browserNamespace).toBe(second.snapshot.state.browserNamespace);
    expect(landing.readLandingWorkspaceState().starting).toBe(true);
    const confirm = vi.spyOn(window, "confirm");
    expect(landing.confirmLandingWorkspaceChange()).toBe(true);
    expect(confirm).not.toHaveBeenCalled();
    expect(discard).not.toHaveBeenCalled();

    if (first.ownsResources) {
      await landing.adoptLandingWorkspace(
        "session-first",
        "host-a",
        "/workspace-a",
        first.snapshot,
      );
    }
    if (second.ownsResources) {
      await landing.adoptLandingWorkspace(
        "session-second",
        "host-a",
        "/workspace-a",
        second.snapshot,
      );
    }

    expect(adopt).toHaveBeenCalledOnce();
    expect(adopt).toHaveBeenCalledWith("session-first");
    expect(browserAdoptDraft).toHaveBeenCalledOnce();
    expect(sessions.readSessionWorkspaceState("session-first")).toMatchObject({
      openTerminals: ["terminal:a"],
      selectedTerminalKey: "terminal:a",
      openBrowsers: ["browser-a"],
      selectedBrowserId: "browser-a",
    });
    expect(sessions.readSessionWorkspaceState("session-second")).toEqual({});

    landing.finishLandingWorkspaceStart(first.token);
    expect(landing.readLandingWorkspaceState().starting).toBe(true);
    landing.finishLandingWorkspaceStart(second.token);
    expect(landing.readLandingWorkspaceState().starting).toBe(false);
  });

  it("lets a rotated namespace own resources while an earlier Start is pending", async () => {
    const landing = await loadLandingWorkspaceState();
    landing.publishLandingWorkspaceSelection({
      hostId: "host-a",
      workspace: "/workspace-a",
      available: true,
      reason: "",
    });
    const discardA = vi.fn().mockResolvedValue(undefined);
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => false,
      discard: discardA,
      adopt: vi.fn().mockResolvedValue(undefined),
    });
    const first = landing.claimLandingWorkspaceStart();

    landing.publishLandingWorkspaceSelection({
      hostId: "host-b",
      workspace: "/workspace-b",
      available: true,
      reason: "",
    });
    expect(landing.captureLandingWorkspace().lifecycle).toBeNull();
    const discardB = vi.fn().mockResolvedValue(undefined);
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => false,
      discard: discardB,
      adopt: vi.fn().mockResolvedValue(undefined),
    });
    const second = landing.claimLandingWorkspaceStart();

    expect(first.ownsResources).toBe(true);
    expect(second.ownsResources).toBe(true);
    expect(first.snapshot.state.browserNamespace).not.toBe(second.snapshot.state.browserNamespace);
    expect(discardA).not.toHaveBeenCalled();
    expect(discardB).not.toHaveBeenCalled();

    landing.finishLandingWorkspaceStart(first.token);
    expect(landing.readLandingWorkspaceState().starting).toBe(true);
    landing.finishLandingWorkspaceStart(second.token);
    expect(landing.readLandingWorkspaceState().starting).toBe(false);
  });

  it("isolates landing drafts by server and signed-in user", async () => {
    const landing = await loadLandingWorkspaceState();
    landing.publishLandingWorkspaceSelection({
      hostId: "host-a",
      workspace: "/server-a/user-a",
      available: true,
      reason: "",
    });

    storageScope.user = "user-b";
    expect(landing.readLandingWorkspaceState().selection).toBeNull();
    landing.publishLandingWorkspaceSelection({
      hostId: "host-b",
      workspace: "/server-a/user-b",
      available: true,
      reason: "",
    });

    storageScope.server = "server-b";
    expect(landing.readLandingWorkspaceState().selection).toBeNull();

    storageScope.server = "server-a";
    storageScope.user = "user-a";
    expect(landing.readLandingWorkspaceState().selection?.workspace).toBe("/server-a/user-a");

    storageScope.user = "user-b";
    expect(landing.readLandingWorkspaceState().selection?.workspace).toBe("/server-a/user-b");
  });

  it("hydrates the new scope before a direct panel write", async () => {
    const landing = await loadLandingWorkspaceState();
    landing.publishLandingWorkspaceSelection({
      hostId: "host-a",
      workspace: "/user-a",
      available: true,
      reason: "",
    });

    storageScope.user = "user-b";
    landing.readLandingWorkspaceState();
    landing.publishLandingWorkspaceSelection({
      hostId: "host-b",
      workspace: "/user-b",
      available: true,
      reason: "",
    });
    landing.writeLandingWorkspacePanel({ openFiles: ["saved-b.ts"] });
    const savedB = landing.readLandingWorkspaceState();

    storageScope.user = "user-a";
    landing.readLandingWorkspaceState();
    storageScope.user = "user-b";
    landing.writeLandingWorkspacePanel({ rightRailTab: "changes" });

    expect(landing.readLandingWorkspaceState()).toMatchObject({
      browserNamespace: savedB.browserNamespace,
      selection: { hostId: "host-b", workspace: "/user-b" },
      panel: { openFiles: ["saved-b.ts"], rightRailTab: "changes" },
    });
  });

  it("does not collide context claims across account scopes", async () => {
    const landing = await loadLandingWorkspaceState();
    landing.publishLandingWorkspaceSelection({
      hostId: "host-a",
      workspace: "/user-a",
      available: true,
      reason: "",
    });
    landing.registerLandingResourceLifecycle({
      contextId: "shared-context-id",
      hasTerminals: () => false,
      discard: vi.fn().mockResolvedValue(undefined),
      adopt: vi.fn().mockResolvedValue(undefined),
    });
    landing.claimLandingWorkspaceStart();

    storageScope.user = "user-b";
    landing.publishLandingWorkspaceSelection({
      hostId: "host-b",
      workspace: "/user-b",
      available: true,
      reason: "",
    });
    landing.registerLandingResourceLifecycle({
      contextId: "shared-context-id",
      hasTerminals: () => false,
      discard: vi.fn().mockResolvedValue(undefined),
      adopt: vi.fn().mockResolvedValue(undefined),
    });
    const namespaceB = landing.readLandingWorkspaceState().browserNamespace;

    expect(landing.landingContextClaimedElsewhere("shared-context-id", namespaceB)).toBe(false);
  });

  it("lets a new scope confirm cleanup while an old scope discard is pending", async () => {
    const landing = await loadLandingWorkspaceState();
    let finishDiscardA!: () => void;
    const discardA = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finishDiscardA = resolve;
        }),
    );
    landing.publishLandingWorkspaceSelection({
      hostId: "host-a",
      workspace: "/user-a",
      available: true,
      reason: "",
    });
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard: discardA,
      adopt: vi.fn().mockResolvedValue(undefined),
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    expect(landing.confirmLandingWorkspaceChange()).toBe(true);

    storageScope.user = "user-b";
    landing.publishLandingWorkspaceSelection({
      hostId: "host-b",
      workspace: "/user-b",
      available: true,
      reason: "",
    });
    const discardB = vi.fn().mockResolvedValue(undefined);
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard: discardB,
      adopt: vi.fn().mockResolvedValue(undefined),
    });

    expect(landing.confirmLandingWorkspaceChange()).toBe(true);
    expect(discardA).toHaveBeenCalledOnce();
    expect(discardB).toHaveBeenCalledOnce();
    finishDiscardA();
    await discardA.mock.results[0]?.value;
  });

  it("closes native browsers and rotates their namespace when the target changes", async () => {
    const landing = await loadLandingWorkspaceState();
    const browserClose = vi.fn().mockResolvedValue({ ok: true });
    const discard = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserClose },
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/first",
      available: true,
      reason: "",
    });
    const hasTerminals = vi.fn(() => false);
    landing.registerLandingResourceLifecycle({
      hasTerminals,
      discard,
      adopt: vi.fn().mockResolvedValue(undefined),
    });
    const firstNamespace = landing.readLandingWorkspaceState().browserNamespace;
    browserClose.mockClear();
    landing.writeLandingWorkspacePanel({
      openFiles: ["first.ts"],
      selectedFilePath: "first.ts",
      selectedTerminalKey: "terminal:first",
      openBrowsers: ["manual one", "manual/two"],
      selectedBrowserId: "manual/two",
      rightRailTab: "changes",
    });

    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/second",
      available: false,
      reason: "The new worktree becomes available after Start.",
    });

    expect(landing.readLandingWorkspaceState()).toMatchObject({
      selection: {
        hostId: "host-1",
        workspace: "/second",
        available: false,
      },
      panel: {
        openFiles: [],
        selectedFilePath: null,
        selectedTerminalKey: null,
        openBrowsers: [],
        selectedBrowserId: null,
        rightRailTab: "changes",
      },
    });
    expect(landing.readLandingWorkspaceState().browserNamespace).not.toBe(firstNamespace);
    expect(hasTerminals).not.toHaveBeenCalled();
    expect(discard).toHaveBeenCalledOnce();
    expect(browserClose.mock.calls).toEqual([
      [firstNamespace],
      [`browser-tab:${encodeURIComponent(firstNamespace)}:manual one`],
      [`browser-tab:${encodeURIComponent(firstNamespace)}:manual/two`],
    ]);
  });

  it("discards draft shells and browsers before clearing the landing workspace", async () => {
    const landing = await loadLandingWorkspaceState();
    const discard = vi.fn().mockResolvedValue(undefined);
    const browserClose = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserClose },
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/workspace",
      available: true,
      reason: "",
    });
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard,
      adopt: vi.fn().mockResolvedValue(undefined),
    });
    const sourceNamespace = landing.readLandingWorkspaceState().browserNamespace;
    browserClose.mockClear();
    landing.writeLandingWorkspacePanel({
      openBrowsers: ["manual-browser"],
      selectedBrowserId: "manual-browser",
      selectedTerminalKey: "terminal:draft",
    });

    await landing.discardLandingWorkspace();

    expect(discard).toHaveBeenCalledOnce();
    expect(browserClose.mock.calls).toEqual([
      [sourceNamespace],
      [`browser-tab:${encodeURIComponent(sourceNamespace)}:manual-browser`],
    ]);
    expect(landing.readLandingWorkspaceState()).toEqual({
      browserNamespace: expect.stringMatching(/^draft-workspace:/),
      selection: null,
      panel: {},
    });
    expect(landing.readLandingWorkspaceState().browserNamespace).not.toBe(sourceNamespace);
  });

  it("preserves the draft when native browser cleanup reports failure", async () => {
    const landing = await loadLandingWorkspaceState();
    const discard = vi.fn().mockResolvedValue(undefined);
    const browserClose = vi.fn((id: string) =>
      Promise.resolve({ ok: !id.startsWith("browser-tab:") }),
    );
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserClose },
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/workspace",
      available: true,
      reason: "",
    });
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => false,
      discard,
      adopt: vi.fn().mockResolvedValue(undefined),
    });
    browserClose.mockClear();
    landing.writeLandingWorkspacePanel({
      openBrowsers: ["browser-fails"],
      selectedBrowserId: "browser-fails",
    });
    const beforeDiscard = landing.readLandingWorkspaceState();

    await expect(landing.discardLandingWorkspace()).rejects.toThrow("Draft browser cleanup failed");

    expect(discard).toHaveBeenCalledOnce();
    expect(browserClose.mock.calls).toEqual([
      [beforeDiscard.browserNamespace],
      [`browser-tab:${encodeURIComponent(beforeDiscard.browserNamespace)}:browser-fails`],
    ]);
    expect(landing.readLandingWorkspaceState()).toEqual(beforeDiscard);
  });

  it("discards a captured workspace without clearing a newer selection", async () => {
    const landing = await loadLandingWorkspaceState();
    const browserClose = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserClose },
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-a",
      workspace: "/workspace-a",
      available: true,
      reason: "",
    });
    landing.writeLandingWorkspacePanel({
      openBrowsers: ["browser-a"],
      selectedBrowserId: "browser-a",
      selectedTerminalKey: "terminal:a",
    });
    const lifecycleA = {
      hasTerminals: vi.fn(() => true),
      discard: vi.fn().mockResolvedValue(undefined),
      adopt: vi.fn().mockResolvedValue(undefined),
    };
    landing.registerLandingResourceLifecycle(lifecycleA);
    const snapshotA = landing.captureLandingWorkspace();

    landing.publishLandingWorkspaceSelection({
      hostId: "host-b",
      workspace: "/workspace-b",
      available: true,
      reason: "",
    });
    lifecycleA.discard.mockClear();
    landing.writeLandingWorkspacePanel({
      openFiles: ["workspace-b.ts"],
      selectedFilePath: "workspace-b.ts",
      openBrowsers: ["browser-b"],
      selectedBrowserId: "browser-b",
    });
    const lifecycleB = {
      hasTerminals: vi.fn(() => true),
      discard: vi.fn().mockResolvedValue(undefined),
      adopt: vi.fn().mockResolvedValue(undefined),
    };
    landing.registerLandingResourceLifecycle(lifecycleB);
    const workspaceB = landing.readLandingWorkspaceState();
    browserClose.mockClear();

    await landing.discardLandingWorkspace(snapshotA);

    expect(lifecycleA.discard).toHaveBeenCalledOnce();
    expect(lifecycleA.adopt).not.toHaveBeenCalled();
    expect(lifecycleB.hasTerminals).not.toHaveBeenCalled();
    expect(lifecycleB.discard).not.toHaveBeenCalled();
    expect(lifecycleB.adopt).not.toHaveBeenCalled();
    expect(browserClose.mock.calls).toEqual([
      [snapshotA.state.browserNamespace],
      [`browser-tab:${encodeURIComponent(snapshotA.state.browserNamespace)}:browser-a`],
    ]);
    expect(landing.readLandingWorkspaceState()).toEqual(workspaceB);
  });

  it("keeps the selected target when changing workspace is cancelled", async () => {
    const landing = await loadLandingWorkspaceState();
    const discard = vi.fn().mockResolvedValue(undefined);
    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/current",
      available: true,
      reason: "",
    });
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard,
      adopt: vi.fn().mockResolvedValue(undefined),
    });
    const beforeConfirm = landing.readLandingWorkspaceState();
    vi.spyOn(window, "confirm").mockReturnValue(false);

    expect(landing.confirmLandingWorkspaceChange()).toBe(false);

    expect(discard).not.toHaveBeenCalled();
    expect(landing.readLandingWorkspaceState()).toEqual(beforeConfirm);
    expect(landing.landingResourceTarget(landing.readLandingWorkspaceState().selection)).toEqual({
      kind: "host",
      hostId: "host-1",
      workspace: "/current",
    });
  });

  it("does not discard the same context twice while confirmed cleanup is still running", async () => {
    const landing = await loadLandingWorkspaceState();
    let finishDiscard!: () => void;
    const discard = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finishDiscard = resolve;
        }),
    );
    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/current",
      available: true,
      reason: "",
    });
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard,
      adopt: vi.fn().mockResolvedValue(undefined),
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    expect(landing.confirmLandingWorkspaceChange()).toBe(true);
    landing.publishLandingWorkspaceSelection({
      hostId: "host-2",
      workspace: "/next",
      available: true,
      reason: "",
    });

    expect(discard).toHaveBeenCalledOnce();
    finishDiscard();
    await discard.mock.results[0]?.value;
  });

  it("does not promise an unavailable retry when confirmed cleanup fails", async () => {
    const landing = await loadLandingWorkspaceState();
    const discard = vi.fn().mockRejectedValue(new Error("host disconnected"));
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard,
      adopt: vi.fn().mockResolvedValue(undefined),
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    expect(landing.confirmLandingWorkspaceChange()).toBe(true);
    await vi.waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Couldn't confirm draft shells closed. Any remaining shells will expire automatically.",
      ),
    );
  });

  it("adopts the draft resources and browser namespace into the created session", async () => {
    const landing = await loadLandingWorkspaceState();
    const sessions = await import("./sessionWorkspaceState");
    const adopt = vi.fn().mockResolvedValue(undefined);
    const browserAdoptDraft = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserAdoptDraft },
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/workspace",
      available: true,
      reason: "",
    });
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard: vi.fn().mockResolvedValue(undefined),
      adopt,
    });
    landing.writeLandingWorkspacePanel({
      rightRailTab: "browser",
      openFiles: ["README.md"],
      openTerminals: ["terminal:draft"],
      selectedTerminalKey: "terminal:draft",
      openBrowsers: ["browser-draft"],
      selectedBrowserId: "browser-draft",
    });
    const sourceNamespace = landing.readLandingWorkspaceState().browserNamespace;

    await landing.adoptLandingWorkspace("session-created", "host-1", "/workspace");

    expect(adopt).toHaveBeenCalledWith("session-created");
    expect(browserAdoptDraft).toHaveBeenCalledWith(sourceNamespace, "session-created");
    expect(sessions.readSessionWorkspaceState("session-created")).toMatchObject({
      rightRailTab: "browser",
      openFiles: ["README.md"],
      openTerminals: ["terminal:draft"],
      selectedTerminalKey: "terminal:draft",
      openBrowsers: ["browser-draft"],
      selectedBrowserId: "browser-draft",
    });
    expect(landing.readLandingWorkspaceState()).toEqual({
      browserNamespace: expect.stringMatching(/^draft-workspace:/),
      selection: null,
      panel: {},
    });
    expect(landing.readLandingWorkspaceState().browserNamespace).not.toBe(sourceNamespace);
  });

  it("adopts a captured workspace without mutating a newer selection", async () => {
    const landing = await loadLandingWorkspaceState();
    const sessions = await import("./sessionWorkspaceState");
    const browserAdoptDraft = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserAdoptDraft },
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-a",
      workspace: "/workspace-a",
      available: true,
      reason: "",
    });
    landing.writeLandingWorkspacePanel({
      openFiles: ["workspace-a.ts"],
      selectedFilePath: "workspace-a.ts",
      openTerminals: ["terminal:a"],
      selectedTerminalKey: "terminal:a",
      openBrowsers: ["browser-a"],
      selectedBrowserId: "browser-a",
    });
    const lifecycleA = {
      hasTerminals: vi.fn(() => true),
      discard: vi.fn().mockResolvedValue(undefined),
      adopt: vi.fn().mockResolvedValue(undefined),
    };
    landing.registerLandingResourceLifecycle(lifecycleA);
    const snapshotA = landing.captureLandingWorkspace();

    landing.publishLandingWorkspaceSelection({
      hostId: "host-b",
      workspace: "/workspace-b",
      available: true,
      reason: "",
    });
    lifecycleA.discard.mockClear();
    landing.writeLandingWorkspacePanel({
      openFiles: ["workspace-b.ts"],
      selectedFilePath: "workspace-b.ts",
      openBrowsers: ["browser-b"],
      selectedBrowserId: "browser-b",
    });
    const lifecycleB = {
      hasTerminals: vi.fn(() => true),
      discard: vi.fn().mockResolvedValue(undefined),
      adopt: vi.fn().mockResolvedValue(undefined),
    };
    landing.registerLandingResourceLifecycle(lifecycleB);
    const workspaceB = landing.readLandingWorkspaceState();

    await landing.adoptLandingWorkspace("session-a", "host-a", "/workspace-a", snapshotA);

    expect(lifecycleA.hasTerminals).toHaveBeenCalledOnce();
    expect(lifecycleA.adopt).toHaveBeenCalledWith("session-a");
    expect(lifecycleA.discard).not.toHaveBeenCalled();
    expect(lifecycleB.hasTerminals).not.toHaveBeenCalled();
    expect(lifecycleB.adopt).not.toHaveBeenCalled();
    expect(lifecycleB.discard).not.toHaveBeenCalled();
    expect(browserAdoptDraft).toHaveBeenCalledWith(snapshotA.state.browserNamespace, "session-a");
    expect(sessions.readSessionWorkspaceState("session-a")).toMatchObject({
      openFiles: ["workspace-a.ts"],
      selectedFilePath: "workspace-a.ts",
      openTerminals: ["terminal:a"],
      selectedTerminalKey: "terminal:a",
      openBrowsers: ["browser-a"],
      selectedBrowserId: "browser-a",
    });
    expect(landing.readLandingWorkspaceState()).toEqual(workspaceB);
  });

  it("keeps adopted shell metadata in the session when browser adoption fails", async () => {
    const landing = await loadLandingWorkspaceState();
    const sessions = await import("./sessionWorkspaceState");
    const adopt = vi.fn().mockResolvedValue(undefined);
    const browserAdoptDraft = vi.fn().mockResolvedValue({ ok: false });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserAdoptDraft },
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/workspace",
      available: true,
      reason: "",
    });
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard: vi.fn().mockResolvedValue(undefined),
      adopt,
    });
    landing.writeLandingWorkspacePanel({
      rightRailTab: "browser",
      openFiles: ["README.md"],
      openTerminals: ["terminal:draft"],
      selectedTerminalKey: "terminal:draft",
      openBrowsers: ["browser-draft"],
      selectedBrowserId: "browser-draft",
    });
    const beforeAdoption = landing.readLandingWorkspaceState();

    await expect(
      landing.adoptLandingWorkspace("session-created", "host-1", "/workspace"),
    ).rejects.toThrow("Draft browser transfer failed");

    expect(adopt).toHaveBeenCalledWith("session-created");
    expect(browserAdoptDraft).toHaveBeenCalledWith(
      beforeAdoption.browserNamespace,
      "session-created",
    );
    expect(landing.readLandingWorkspaceState()).toEqual(beforeAdoption);
    expect(sessions.readSessionWorkspaceState("session-created")).toMatchObject({
      rightRailTab: "browser",
      openFiles: ["README.md"],
      openTerminals: ["terminal:draft"],
      selectedTerminalKey: "terminal:draft",
      openBrowsers: [],
      selectedBrowserId: null,
    });
  });

  it("leaves the session untouched when a browser-only draft fails to transfer", async () => {
    const landing = await loadLandingWorkspaceState();
    const sessions = await import("./sessionWorkspaceState");
    const adopt = vi.fn().mockResolvedValue(undefined);
    const browserAdoptDraft = vi.fn().mockResolvedValue({ ok: false });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserAdoptDraft },
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/workspace",
      available: true,
      reason: "",
    });
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => false,
      discard: vi.fn().mockResolvedValue(undefined),
      adopt,
    });
    landing.writeLandingWorkspacePanel({
      rightRailTab: "browser",
      openBrowsers: ["browser-draft"],
      selectedBrowserId: "browser-draft",
    });
    const beforeAdoption = landing.readLandingWorkspaceState();

    await expect(
      landing.adoptLandingWorkspace("session-created", "host-1", "/workspace"),
    ).rejects.toThrow("Draft browser transfer failed");

    expect(adopt).toHaveBeenCalledWith("session-created");
    expect(browserAdoptDraft).toHaveBeenCalledWith(
      beforeAdoption.browserNamespace,
      "session-created",
    );
    expect(landing.readLandingWorkspaceState()).toEqual(beforeAdoption);
    expect(sessions.readSessionWorkspaceState("session-created")).toEqual({});
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it("does not clean an abandoned snapshot while it is still the current workspace", async () => {
    const landing = await loadLandingWorkspaceState();
    const discard = vi.fn().mockRejectedValue(new Error("must not run"));
    const browserClose = vi.fn().mockResolvedValue({ ok: false });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserClose },
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-a",
      workspace: "/workspace-a",
      available: true,
      reason: "",
    });
    browserClose.mockClear();
    landing.writeLandingWorkspacePanel({ openBrowsers: ["browser-a"] });
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard,
      adopt: vi.fn().mockResolvedValue(undefined),
    });
    const snapshot = landing.captureLandingWorkspace();
    const beforeCleanup = landing.readLandingWorkspaceState();

    await expect(landing.discardAbandonedLandingWorkspace(snapshot)).resolves.toBeUndefined();

    expect(discard).not.toHaveBeenCalled();
    expect(browserClose).not.toHaveBeenCalled();
    expect(landing.readLandingWorkspaceState()).toEqual(beforeCleanup);
  });

  it("marks failed terminal adoption untransferred and still attempts obsolete browser cleanup", async () => {
    const landing = await loadLandingWorkspaceState();
    const adopt = vi.fn().mockRejectedValue(new Error("terminal handoff failed"));
    const discard = vi.fn().mockRejectedValue(new Error("terminal cleanup failed"));
    const browserAdoptDraft = vi.fn().mockResolvedValue({ ok: true });
    const browserClose = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserAdoptDraft, browserClose },
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-a",
      workspace: "/workspace-a",
      available: true,
      reason: "",
    });
    browserClose.mockClear();
    landing.writeLandingWorkspacePanel({ openBrowsers: ["browser-a"] });
    landing.registerLandingResourceLifecycle({
      contextId: "context-a",
      hasTerminals: () => true,
      discard,
      adopt,
    });
    const claim = landing.claimLandingWorkspaceStart();
    landing.publishLandingWorkspaceSelection({
      hostId: "host-b",
      workspace: "/workspace-b",
      available: true,
      reason: "",
    });
    landing.writeLandingWorkspacePanel({ openFiles: ["workspace-b.ts"] });
    const workspaceB = landing.readLandingWorkspaceState();

    let failure: unknown;
    try {
      await landing.adoptLandingWorkspace("session-a", "host-a", "/workspace-a", claim.snapshot);
    } catch (error) {
      failure = error;
    }

    expect(failure).toBeInstanceOf(landing.LandingWorkspaceAdoptionError);
    expect(failure).toMatchObject({
      terminalsTransferred: false,
      message: "terminal handoff failed",
    });
    expect(browserAdoptDraft).not.toHaveBeenCalled();
    await expect(
      landing.discardAbandonedLandingWorkspace(
        claim.snapshot,
        (failure as { terminalsTransferred: boolean }).terminalsTransferred,
      ),
    ).rejects.toThrow("terminal cleanup failed");
    expect(discard).toHaveBeenCalledOnce();
    expect(browserClose.mock.calls).toEqual([
      [claim.snapshot.state.browserNamespace],
      [`browser-tab:${encodeURIComponent(claim.snapshot.state.browserNamespace)}:browser-a`],
    ]);
    expect(landing.readLandingWorkspaceState()).toEqual(workspaceB);
    landing.finishLandingWorkspaceStart(claim.token);
  });

  it("preserves adopted terminals when an obsolete browser transfer fails", async () => {
    const landing = await loadLandingWorkspaceState();
    const sessions = await import("./sessionWorkspaceState");
    const adopt = vi.fn().mockResolvedValue(undefined);
    const discard = vi.fn().mockResolvedValue(undefined);
    const browserAdoptDraft = vi.fn().mockRejectedValue(new Error("browser handoff failed"));
    const browserClose = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserAdoptDraft, browserClose },
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-a",
      workspace: "/workspace-a",
      available: true,
      reason: "",
    });
    browserClose.mockClear();
    landing.writeLandingWorkspacePanel({
      openTerminals: ["terminal:a"],
      selectedTerminalKey: "terminal:a",
      openBrowsers: ["browser-a"],
      selectedBrowserId: "browser-a",
    });
    landing.registerLandingResourceLifecycle({
      contextId: "context-a",
      hasTerminals: () => true,
      discard,
      adopt,
    });
    const claim = landing.claimLandingWorkspaceStart();
    landing.publishLandingWorkspaceSelection({
      hostId: "host-b",
      workspace: "/workspace-b",
      available: true,
      reason: "",
    });
    landing.writeLandingWorkspacePanel({ openFiles: ["workspace-b.ts"] });
    const workspaceB = landing.readLandingWorkspaceState();

    let failure: unknown;
    try {
      await landing.adoptLandingWorkspace("session-a", "host-a", "/workspace-a", claim.snapshot);
    } catch (error) {
      failure = error;
    }

    expect(failure).toBeInstanceOf(landing.LandingWorkspaceAdoptionError);
    expect(failure).toMatchObject({
      terminalsTransferred: true,
      message: "browser handoff failed",
    });
    expect(sessions.readSessionWorkspaceState("session-a")).toMatchObject({
      openTerminals: ["terminal:a"],
      selectedTerminalKey: "terminal:a",
      openBrowsers: [],
      selectedBrowserId: null,
    });
    await landing.discardAbandonedLandingWorkspace(
      claim.snapshot,
      (failure as { terminalsTransferred: boolean }).terminalsTransferred,
    );
    expect(discard).not.toHaveBeenCalled();
    expect(browserClose.mock.calls).toEqual([
      [claim.snapshot.state.browserNamespace],
      [`browser-tab:${encodeURIComponent(claim.snapshot.state.browserNamespace)}:browser-a`],
    ]);
    expect(landing.readLandingWorkspaceState()).toEqual(workspaceB);
    landing.finishLandingWorkspaceStart(claim.token);
  });

  it("does not adopt draft resources into a session created for another target", async () => {
    const landing = await loadLandingWorkspaceState();
    const adopt = vi.fn().mockResolvedValue(undefined);
    const browserAdoptDraft = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserAdoptDraft },
    });
    landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard: vi.fn().mockResolvedValue(undefined),
      adopt,
    });
    landing.publishLandingWorkspaceSelection({
      hostId: "host-1",
      workspace: "/draft",
      available: true,
      reason: "",
    });

    await landing.adoptLandingWorkspace("session-created", "host-2", "/other");

    expect(adopt).not.toHaveBeenCalled();
    expect(browserAdoptDraft).not.toHaveBeenCalled();
    expect(landing.readLandingWorkspaceState().selection?.workspace).toBe("/draft");
  });
});
