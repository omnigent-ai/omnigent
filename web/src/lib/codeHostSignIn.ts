// Signing in to GitHub on this Mac, from the page's side. Main runs gh's device flow after its
// own confirmation; this controller drives the dialog from main's progress events, keyed by
// the request ID the preload made, so events that arrive before main's reply still count and
// events for any other request are ignored.

import { createStore, type StoreApi } from "zustand/vanilla";
import {
  githubSignInBridge,
  type GitHubSignInBridge,
  type GitHubSignInProgress,
  type GitHubSignInStorage,
} from "@/lib/nativeBridge";

export type GitHubSignInResult = { ok: true; login: string } | { ok: false; code: string };

export type GitHubSignInState =
  | { phase: "closed" }
  | { phase: "choosing"; hosts: string[] | null; error: string | null }
  | { phase: "intro"; host: string }
  | { phase: "confirming"; host: string; requestId: string }
  | { phase: "code"; host: string; requestId: string; userCode: string; waiting: boolean }
  | {
      phase: "done";
      host: string;
      outcome: "confirmed_new" | "confirmed_renewed" | "changed_unverified";
      login: string | null;
      storage: GitHubSignInStorage | null;
      /** A sign-out of this account from the dialog: running, or its result. */
      signOut: { running: true } | { running: false; result: SignOutResult } | null;
    }
  | { phase: "failed"; host: string; code: string };

export interface GitHubSignInController {
  store: StoreApi<GitHubSignInState>;
  /** Open the dialog, for `host` or with a host picker; resolves when the dialog closes. */
  open: (request?: { host?: string }) => Promise<GitHubSignInResult>;
  chooseHost: (host: string) => void;
  /** Continue from the intro: main asks for its confirmation, then starts gh. */
  begin: () => void;
  openVerification: () => void;
  /** Back to the intro after a failure. */
  retry: () => void;
  /** Close the dialog, cancelling a sign-in that's still running. */
  close: () => void;
  /** Whether this shell can sign accounts out (an older one may only sign in). */
  canSignOut: () => boolean;
  /** Sign out the account this dialog just signed in; the dialog's result follows it. */
  signOutSignedInAccount: () => Promise<void>;
}

export interface SignOutResult {
  ok: boolean;
  outcome?: string;
  code?: string;
}

const RUNNING_PHASES = new Set(["confirming", "code"]);

// A sign-out that ended with one of these left the account where it was.
const SIGN_OUT_LEFT_ACCOUNT = new Set([
  "cancelled",
  "sign_out_failed",
  "busy",
  "needs_user_gesture",
  "unavailable",
  "disabled",
]);

function resultOf(state: GitHubSignInState): GitHubSignInResult {
  if (state.phase === "done") {
    if (state.outcome === "changed_unverified" || !state.login) {
      return { ok: false, code: "unverified" };
    }
    const signOut = state.signOut;
    // Closing while the sign-out runs can't promise the account is still there.
    if (signOut?.running) return { ok: false, code: "unverified" };
    if (signOut && !signOut.running) {
      const { result } = signOut;
      if (result.ok && result.outcome === "signed_out") return { ok: false, code: "signed_out" };
      if (result.ok || !SIGN_OUT_LEFT_ACCOUNT.has(result.code ?? "")) {
        return { ok: false, code: "unverified" };
      }
    }
    return { ok: true, login: state.login };
  }
  if (state.phase === "failed") return { ok: false, code: state.code };
  return { ok: false, code: "cancelled" };
}

export function createGitHubSignInController(
  bridge: () => GitHubSignInBridge | null,
): GitHubSignInController {
  const store = createStore<GitHubSignInState>(() => ({ phase: "closed" }));
  const show = (next: GitHubSignInState) => store.setState(next, true);
  let settle: ((result: GitHubSignInResult) => void) | null = null;
  let pending: Promise<GitHubSignInResult> | null = null;
  let unsubscribe: (() => void) | null = null;

  const currentRequest = (): string | null => {
    const state = store.getState();
    return "requestId" in state ? state.requestId : null;
  };

  function onProgress(progress: GitHubSignInProgress) {
    const state = store.getState();
    if (!("requestId" in state) || state.requestId !== progress.requestId) return;
    const { host, requestId } = state;
    if (progress.state === "code" && progress.userCode) {
      show({ phase: "code", host, requestId, userCode: progress.userCode, waiting: false });
    } else if (progress.state === "waiting" && state.phase === "code") {
      show({ ...state, waiting: true });
    } else if (progress.state === "done") {
      const outcome =
        progress.outcome === "confirmed_new" || progress.outcome === "confirmed_renewed"
          ? progress.outcome
          : "changed_unverified";
      show({
        phase: "done",
        host,
        outcome,
        login: progress.login ?? null,
        storage: progress.storage ?? null,
        signOut: null,
      });
    } else if (progress.state === "failed") {
      if (progress.code === "cancelled") finish({ ok: false, code: "cancelled" });
      else show({ phase: "failed", host, code: progress.code ?? "sign_in_failed" });
    }
  }

  function listen(signIn: GitHubSignInBridge) {
    if (!unsubscribe) unsubscribe = signIn.onProgress(onProgress);
  }

  function finish(result: GitHubSignInResult) {
    show({ phase: "closed" });
    unsubscribe?.();
    unsubscribe = null;
    const resolve = settle;
    settle = null;
    pending = null;
    resolve?.(result);
  }

  async function loadHosts(signIn: GitHubSignInBridge) {
    let accounts;
    try {
      accounts = await signIn.accounts();
    } catch {
      accounts = null;
    }
    const state = store.getState();
    if (state.phase !== "choosing") return;
    if (!accounts || accounts.status !== "ok") {
      show({ phase: "choosing", hosts: [], error: "Couldn't read GitHub sign-ins on this Mac." });
      return;
    }
    const hosts = accounts.hosts.map((entry) => entry.host);
    if (hosts.length === 1) show({ phase: "intro", host: hosts[0] });
    else show({ phase: "choosing", hosts, error: null });
  }

  return {
    store,
    open(request = {}) {
      if (pending) return pending;
      const signIn = bridge();
      if (!signIn) return Promise.resolve({ ok: false, code: "unavailable" });
      pending = new Promise<GitHubSignInResult>((resolve) => {
        settle = resolve;
      });
      listen(signIn);
      if (request.host) show({ phase: "intro", host: request.host.toLowerCase() });
      else {
        show({ phase: "choosing", hosts: null, error: null });
        void loadHosts(signIn);
      }
      return pending;
    },
    chooseHost(host) {
      if (store.getState().phase === "choosing") show({ phase: "intro", host });
    },
    begin() {
      const state = store.getState();
      const signIn = bridge();
      if (state.phase !== "intro" || !signIn) return;
      const { requestId, reply } = signIn.start({ provider: "github", host: state.host });
      if (!requestId) {
        void reply.then(({ code }) => {
          if (store.getState().phase === "intro") {
            show({ phase: "failed", host: state.host, code: code ?? "needs_user_gesture" });
          }
        });
        return;
      }
      show({ phase: "confirming", host: state.host, requestId });
      void reply.then(
        ({ ok, code }) => {
          if (!ok && currentRequest() === requestId) {
            show({ phase: "failed", host: state.host, code: code ?? "sign_in_failed" });
          }
        },
        () => {
          if (currentRequest() === requestId) {
            show({ phase: "failed", host: state.host, code: "unsupported" });
          }
        },
      );
    },
    openVerification() {
      const requestId = currentRequest();
      const signIn = bridge();
      if (requestId && signIn) void signIn.openVerification(requestId).catch(() => false);
    },
    retry() {
      const state = store.getState();
      if (state.phase === "failed") show({ phase: "intro", host: state.host });
    },
    close() {
      const state = store.getState();
      if (state.phase === "closed") return;
      const requestId = currentRequest();
      if (requestId && RUNNING_PHASES.has(state.phase)) bridge()?.cancel(requestId);
      finish(resultOf(state));
    },
    canSignOut: () => typeof bridge()?.signOut === "function",
    async signOutSignedInAccount() {
      const state = store.getState();
      if (state.phase !== "done" || !state.login || state.signOut?.running) return;
      const signingOut = { ...state, signOut: { running: true as const } };
      show(signingOut);
      const result = await signOutOfGitHubOnThisMac(
        { host: state.host, login: state.login },
        bridge,
      );
      // The dialog may have closed, or moved on, meanwhile.
      if (store.getState() === signingOut) {
        show({ ...signingOut, signOut: { running: false, result } });
      }
    },
  };
}

export const githubSignInController = createGitHubSignInController(githubSignInBridge);

/**
 * Sign in to GitHub on this Mac for `host`: opens the dialog and resolves when it closes.
 * Anything but `ok: true` means the Mac didn't get a confirmed sign-in.
 */
export function signInToGitHubOnThisMac({ host }: { host: string }): Promise<GitHubSignInResult> {
  return githubSignInController.open({ host });
}

/**
 * Sign a plaintext-stored account out of the GitHub CLI on this Mac. Main asks for its own
 * confirmation and re-checks the account first. Resolves with the outcome or failure code.
 */
export function signOutOfGitHubOnThisMac(
  { host, login }: { host: string; login: string },
  bridge: () => GitHubSignInBridge | null = githubSignInBridge,
): Promise<SignOutResult> {
  const signIn = bridge();
  const signOut = signIn?.signOut;
  if (!signIn || !signOut) return Promise.resolve({ ok: false, code: "unavailable" });
  return new Promise((resolve) => {
    let requestId: string | null = null;
    const early: GitHubSignInProgress[] = [];
    const stop = signIn.onProgress((progress) => {
      if (requestId === null) early.push(progress);
      else handle(progress);
    });
    const done = (result: SignOutResult) => {
      stop();
      resolve(result);
    };
    function handle(progress: GitHubSignInProgress) {
      if (progress.requestId !== requestId) return;
      if (progress.state === "done") done({ ok: true, outcome: progress.outcome });
      else if (progress.state === "failed") done({ ok: false, code: progress.code });
    }
    const request = signOut({ provider: "github", host, login });
    requestId = request.requestId;
    if (!requestId) {
      void request.reply.then(({ code }) =>
        done({ ok: false, code: code ?? "needs_user_gesture" }),
      );
      return;
    }
    for (const progress of early) handle(progress);
    void request.reply.then(
      ({ ok, code }) => {
        if (!ok) done({ ok: false, code: code ?? "sign_out_failed" });
      },
      () => done({ ok: false, code: "unsupported" }),
    );
  });
}
