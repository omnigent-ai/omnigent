import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createGitHubSignInController,
  signOutOfGitHubOnThisMac,
  type GitHubSignInController,
} from "./codeHostSignIn";
import {
  githubSignInBridge,
  isGitHubSignInAvailable,
  type GitHubSignInBridge,
  type GitHubSignInProgress,
} from "./nativeBridge";

const REQUEST = "00000000-0000-4000-8000-000000000001";

/** A bridge whose progress and replies the test drives by hand. */
function fakeBridge({
  requestId = REQUEST as string | null,
  reply = { ok: true } as { ok: boolean; code?: string },
} = {}) {
  const listeners = new Set<(progress: GitHubSignInProgress) => void>();
  const bridge: GitHubSignInBridge = {
    accounts: vi.fn(async () => ({
      status: "ok" as const,
      hosts: [{ host: "github.com", kind: "github.com" as const, accounts: [] }],
    })),
    start: vi.fn(() => ({ requestId, reply: Promise.resolve(reply) })),
    signOut: vi.fn(() => ({ requestId, reply: Promise.resolve(reply) })),
    openVerification: vi.fn(async () => true),
    cancel: vi.fn(),
    onProgress: vi.fn((callback) => {
      listeners.add(callback);
      return () => listeners.delete(callback);
    }),
    onAccountsChanged: vi.fn(() => () => {}),
  };
  const emit = (progress: Omit<GitHubSignInProgress, "requestId"> & { requestId?: string }) => {
    for (const listener of [...listeners]) listener({ requestId: REQUEST, ...progress });
  };
  return { bridge, emit, listeners };
}

const flush = () =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

function begin(controller: GitHubSignInController) {
  const result = controller.open({ host: "GitHub.com" });
  controller.begin();
  return result;
}

describe("GitHub sign-in controller", () => {
  it("is unavailable without a bridge", async () => {
    const controller = createGitHubSignInController(() => null);
    await expect(controller.open({ host: "github.com" })).resolves.toEqual({
      ok: false,
      code: "unavailable",
    });
    expect(controller.store.getState()).toEqual({ phase: "closed" });
  });

  it("goes from intro to code to a confirmed sign-in, and resolves when closed", async () => {
    const { bridge, emit, listeners } = fakeBridge();
    const controller = createGitHubSignInController(() => bridge);
    const result = controller.open({ host: "GitHub.com" });
    expect(controller.store.getState()).toEqual({ phase: "intro", host: "github.com" });
    controller.begin();
    expect(bridge.start).toHaveBeenCalledWith({ provider: "github", host: "github.com" });
    expect(controller.store.getState()).toMatchObject({ phase: "confirming", requestId: REQUEST });
    emit({ state: "code", userCode: "ABCD-1234" });
    emit({ state: "waiting" });
    expect(controller.store.getState()).toMatchObject({
      phase: "code",
      userCode: "ABCD-1234",
      waiting: true,
    });
    controller.openVerification();
    expect(bridge.openVerification).toHaveBeenCalledWith(REQUEST);
    emit({ state: "done", outcome: "confirmed_new", login: "octo", storage: "keychain" });
    expect(controller.store.getState()).toMatchObject({ phase: "done", login: "octo" });
    controller.close();
    await expect(result).resolves.toEqual({ ok: true, login: "octo" });
    expect(listeners.size).toBe(0);
    expect(bridge.cancel).not.toHaveBeenCalled();
  });

  it("uses progress that arrives before main's reply", async () => {
    let answer: (value: { ok: boolean }) => void = () => {};
    const { bridge, emit } = fakeBridge();
    bridge.start = vi.fn(() => ({
      requestId: REQUEST,
      reply: new Promise<{ ok: boolean }>((resolve) => {
        answer = resolve;
      }),
    }));
    const controller = createGitHubSignInController(() => bridge);
    void begin(controller);
    emit({ state: "code", userCode: "WXYZ-9876" });
    answer({ ok: true });
    await flush();
    expect(controller.store.getState()).toMatchObject({ phase: "code", userCode: "WXYZ-9876" });
  });

  it("ignores progress for any other request, and late events after closing", async () => {
    const { bridge, emit } = fakeBridge();
    const controller = createGitHubSignInController(() => bridge);
    const result = begin(controller);
    emit({
      requestId: "00000000-0000-4000-8000-000000000002",
      state: "code",
      userCode: "NOPE-0000",
    });
    expect(controller.store.getState().phase).toBe("confirming");
    controller.close();
    expect(bridge.cancel).toHaveBeenCalledWith(REQUEST);
    await expect(result).resolves.toEqual({ ok: false, code: "cancelled" });
    emit({ state: "done", outcome: "confirmed_new", login: "late" });
    expect(controller.store.getState()).toEqual({ phase: "closed" });
  });

  it("reports an unverified change as not signed in", async () => {
    const { bridge, emit } = fakeBridge();
    const controller = createGitHubSignInController(() => bridge);
    const result = begin(controller);
    emit({ state: "done", outcome: "changed_unverified" });
    controller.close();
    await expect(result).resolves.toEqual({ ok: false, code: "unverified" });
  });

  it("shows failures, retries from the intro, and closes on main's cancel", async () => {
    const { bridge, emit } = fakeBridge();
    const controller = createGitHubSignInController(() => bridge);
    const result = begin(controller);
    emit({ state: "failed", code: "denied" });
    expect(controller.store.getState()).toEqual({
      phase: "failed",
      host: "github.com",
      code: "denied",
    });
    controller.retry();
    expect(controller.store.getState()).toEqual({ phase: "intro", host: "github.com" });
    controller.begin();
    emit({ state: "failed", code: "cancelled" });
    await expect(result).resolves.toEqual({ ok: false, code: "cancelled" });
  });

  it("shows a refused start, including one without a user gesture", async () => {
    let harness = fakeBridge({ reply: { ok: false, code: "busy" } });
    let controller = createGitHubSignInController(() => harness.bridge);
    void begin(controller);
    await flush();
    expect(controller.store.getState()).toMatchObject({ phase: "failed", code: "busy" });

    harness = fakeBridge({ requestId: null, reply: { ok: false, code: "needs_user_gesture" } });
    controller = createGitHubSignInController(() => harness.bridge);
    void begin(controller);
    await flush();
    expect(controller.store.getState()).toMatchObject({
      phase: "failed",
      code: "needs_user_gesture",
    });
  });

  it("asks main for hosts without one, and skips the picker when there's one host", async () => {
    const { bridge } = fakeBridge();
    const controller = createGitHubSignInController(() => bridge);
    void controller.open();
    expect(controller.store.getState()).toEqual({ phase: "choosing", hosts: null, error: null });
    await flush();
    expect(controller.store.getState()).toEqual({ phase: "intro", host: "github.com" });
    controller.close();

    bridge.accounts = vi.fn(async () => ({
      status: "ok" as const,
      hosts: [
        { host: "github.com", kind: "github.com" as const, accounts: [] },
        { host: "ghe.example.com", kind: "enterprise" as const, accounts: [] },
      ],
    }));
    void controller.open();
    await flush();
    expect(controller.store.getState()).toEqual({
      phase: "choosing",
      hosts: ["github.com", "ghe.example.com"],
      error: null,
    });
    controller.chooseHost("ghe.example.com");
    expect(controller.store.getState()).toEqual({ phase: "intro", host: "ghe.example.com" });
    controller.close();

    bridge.accounts = vi.fn(async () => ({ status: "unavailable" as const, code: "unreadable" }));
    void controller.open();
    await flush();
    expect(controller.store.getState()).toMatchObject({ phase: "choosing", hosts: [] });
  });

  it("returns the open dialog's result to a second caller", async () => {
    const { bridge } = fakeBridge();
    const controller = createGitHubSignInController(() => bridge);
    const first = controller.open({ host: "github.com" });
    const second = controller.open({ host: "ghe.example.com" });
    expect(second).toBe(first);
    controller.close();
    await expect(first).resolves.toEqual({ ok: false, code: "cancelled" });
  });
});

describe("signing out from the done step", () => {
  async function signedIn() {
    const harness = fakeBridge();
    const controller = createGitHubSignInController(() => harness.bridge);
    const result = begin(controller);
    harness.emit({ state: "done", outcome: "confirmed_new", login: "plain", storage: "insecure" });
    return { ...harness, controller, result };
  }

  it("reports the account signed out after a completed sign-out", async () => {
    const { controller, emit, result } = await signedIn();
    const signingOut = controller.signOutSignedInAccount();
    expect(controller.store.getState()).toMatchObject({ signOut: { running: true } });
    emit({ state: "done", outcome: "signed_out", login: "plain" });
    await signingOut;
    controller.close();
    await expect(result).resolves.toEqual({ ok: false, code: "signed_out" });
  });

  it("doesn't promise a sign-in while the sign-out is still running", async () => {
    const { controller, emit, result } = await signedIn();
    void controller.signOutSignedInAccount();
    controller.close();
    await expect(result).resolves.toEqual({ ok: false, code: "unverified" });
    emit({ state: "done", outcome: "signed_out", login: "plain" });
    expect(controller.store.getState()).toEqual({ phase: "closed" });
  });

  it("keeps the sign-in when the sign-out failed, and is unsure when it may have changed", async () => {
    let harness = await signedIn();
    let signingOut = harness.controller.signOutSignedInAccount();
    harness.emit({ state: "failed", code: "sign_out_failed" });
    await signingOut;
    harness.controller.close();
    await expect(harness.result).resolves.toEqual({ ok: true, login: "plain" });

    harness = await signedIn();
    signingOut = harness.controller.signOutSignedInAccount();
    harness.emit({ state: "done", outcome: "sign_out_unverified" });
    await signingOut;
    harness.controller.close();
    await expect(harness.result).resolves.toEqual({ ok: false, code: "unverified" });
  });

  it("knows whether the shell can sign out", () => {
    const { bridge } = fakeBridge();
    expect(createGitHubSignInController(() => bridge).canSignOut()).toBe(true);
    expect(
      createGitHubSignInController(() => ({ ...bridge, signOut: undefined })).canSignOut(),
    ).toBe(false);
  });
});

describe("signing out on this Mac", () => {
  it("resolves with main's outcome for its own request", async () => {
    const { bridge, emit } = fakeBridge();
    const result = signOutOfGitHubOnThisMac({ host: "github.com", login: "plain" }, () => bridge);
    emit({
      requestId: "00000000-0000-4000-8000-000000000002",
      state: "done",
      outcome: "signed_out",
    });
    emit({ state: "done", outcome: "signed_out", login: "plain" });
    await expect(result).resolves.toEqual({ ok: true, outcome: "signed_out" });
    expect(bridge.signOut).toHaveBeenCalledWith({
      provider: "github",
      host: "github.com",
      login: "plain",
    });
  });

  it("reports failures and an older shell without sign-out", async () => {
    const { bridge, emit } = fakeBridge();
    const result = signOutOfGitHubOnThisMac({ host: "github.com", login: "plain" }, () => bridge);
    emit({ state: "failed", code: "stale" });
    await expect(result).resolves.toEqual({ ok: false, code: "stale" });
    await expect(
      signOutOfGitHubOnThisMac({ host: "github.com", login: "x" }, () => ({
        ...bridge,
        signOut: undefined,
      })),
    ).resolves.toEqual({ ok: false, code: "unavailable" });
  });
});

describe("the sign-in bridge on older and partial shells", () => {
  const install = (desktop: Record<string, unknown> | undefined) => {
    const target = window as unknown as Record<string, unknown>;
    if (desktop) target.omnigentDesktop = { kind: "electron", ...desktop };
    else delete target.omnigentDesktop;
  };
  afterEach(() => install(undefined));

  const methods = () => ({
    accounts: vi.fn(),
    start: vi.fn(),
    openVerification: vi.fn(),
    cancel: vi.fn(),
    onProgress: vi.fn(),
    onAccountsChanged: vi.fn(),
  });

  it("needs every sign-in method, but not signOut", () => {
    install(undefined);
    expect(githubSignInBridge()).toBeNull();
    install({});
    expect(githubSignInBridge()).toBeNull();
    const { cancel: _missing, ...partial } = methods();
    install({ codeHostSignIn: partial });
    expect(githubSignInBridge()).toBeNull();
    install({ codeHostSignIn: methods() });
    expect(githubSignInBridge()?.signOut).toBeUndefined();
    install({ codeHostSignIn: { ...methods(), signOut: vi.fn() } });
    expect(typeof githubSignInBridge()?.signOut).toBe("function");
  });

  it("is available only when the github entry says sign_in", async () => {
    install({
      codeHostSignIn: methods(),
      getDesktopFeatures: async () => ({
        codeHosts: [{ id: "github", operations: [], sign_in: true }],
      }),
    });
    await expect(isGitHubSignInAvailable()).resolves.toBe(true);
    install({
      codeHostSignIn: methods(),
      getDesktopFeatures: async () => ({ codeHosts: [{ id: "github", operations: [] }] }),
    });
    await expect(isGitHubSignInAvailable()).resolves.toBe(false);
    install({ codeHostSignIn: methods(), getDesktopFeatures: async () => ({}) });
    await expect(isGitHubSignInAvailable()).resolves.toBe(false);
    install({ getDesktopFeatures: async () => ({ codeHosts: [{ id: "github", sign_in: true }] }) });
    await expect(isGitHubSignInAvailable()).resolves.toBe(false);
  });
});
