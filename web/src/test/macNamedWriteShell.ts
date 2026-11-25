// A stand-in desktop shell for writes as a login the Pull Requests page reads as by name: it reads
// github.com as `alice` (gh's active account) and, by name, as `bob`, and writes as either. A
// shell can leave out `writeAs` or the `codeHostWriteAs` method, as an older or partial one does.

import { expect, vi } from "vitest";

import { readOnThisMac } from "@/lib/desktopPullRequests";
import {
  GITHUB_WRITE_SET_V1,
  REVIEW_WRITE_OPERATIONS,
  setMacWritesReleaseFlagForTests,
  setPageWriteOperationsForTests,
  setReviewsFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import {
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  macReadTargetAs,
  refreshDesktopReadSupport,
} from "@/lib/desktopReadPath";
import {
  namedReaderChanged,
  namedReadsAvailable,
  PULL_REQUESTS_PAGE_ACCOUNTS_FLAG,
  type MacNamedReader,
} from "@/lib/macNamedReads";

export const HOST_LOGIN = "alice";
export const BOB: MacNamedReader = { host: "github.com", login: "bob" };
const PR_PARAMS = { repository: "acme/app", number: 42 };

export interface NamedWriteShellOptions {
  /** The shell advertises `codeHosts[github].writeAs`; false for an older shell. */
  writeAs?: boolean;
  /** The preload has `codeHostWriteAs`; false for an older or partial bridge. */
  writeAsMethod?: boolean;
  /** The generation main answers a read by name with, Bob's or any login's. */
  bobGeneration?: number;
  /** Writes it offers beyond the v1 set and reviews, e.g. comment edits. */
  extraWrites?: readonly string[];
  /** What a read answers, by operation, for the login it ran as; `{}` otherwise. */
  reads?: Record<string, (login: string) => unknown>;
  /** Reads it doesn't list, as an older shell doesn't. */
  omitReads?: readonly string[];
}

/** Install the shell; `answer` answers each write, as either method. */
export function installNamedWriteShell(
  answer: (request: Record<string, unknown>) => unknown = () => ({ ok: false }),
  {
    writeAs = true,
    writeAsMethod = true,
    bobGeneration = 0,
    extraWrites = [],
    reads = {},
    omitReads = [],
  }: NamedWriteShellOptions = {},
) {
  const flag: { set: ((value: unknown) => void) | null } = { set: null };
  const state = {
    readAs: true,
    writeAs,
    bobGeneration,
    bobSignedIn: true,
    hostLogin: HOST_LOGIN,
    hostGeneration: 1,
  };
  // Reads held mid-flight, by operation, until a test lets them answer.
  const paused = new Map<string, Promise<void>>();
  const codeHostQuery = vi.fn(async (request: Record<string, unknown>) => {
    const hold = paused.get(request.operation as string);
    if (hold) {
      paused.delete(request.operation as string);
      await hold;
    }
    if (request.operation === "account_list") {
      return { ok: true, data: [{ login: state.hostLogin, active: true, state: "success" }] };
    }
    const named = typeof request.macLogin === "string";
    if (named && !state.bobSignedIn) {
      return { ok: false, error: { code: "account_missing", message: "@bob isn't signed in." } };
    }
    const login = named ? (request.macLogin as string) : state.hostLogin;
    return {
      ok: true,
      data: reads[request.operation as string]?.(login) ?? {},
      identity: named
        ? { host: "github.com", login, generation: state.bobGeneration, named: true }
        : { host: "github.com", login, generation: state.hostGeneration },
      provenance: { version: 1, provider: "github", host: "github.com", viewer: login },
    };
  });
  const codeHostWrite = vi.fn(async (request: Record<string, unknown>) => answer(request));
  const codeHostWriteAs = vi.fn(async (request: Record<string, unknown>) => answer(request));
  const writeOperations = [...GITHUB_WRITE_SET_V1, ...REVIEW_WRITE_OPERATIONS, ...extraWrites];
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery,
    codeHostWrite,
    ...(writeAsMethod ? { codeHostWriteAs } : {}),
    codeHostWriteCancel: vi.fn(),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
      onNamedChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [
            ...GITHUB_MAC_READ_OPERATIONS,
            "pull_request_label_options",
            "pull_request_review_candidates",
            "pull_request_merge_options",
            "pull_request_comment_state",
            "pull_request_comment_reactions",
            "pull_request_review_check",
            "pull_request_pending_review",
            "pull_request_review_state",
          ].filter((operation) => !omitReads.includes(operation)),
          accounts: true,
          answerProvenance: 1,
          ...(state.readAs ? { readAs: 1 } : {}),
          writes: { version: 1, operations: writeOperations },
          ...(state.writeAs ? { writeAs: 1 } : {}),
        },
      ],
      featureFlags: { names: [PULL_REQUESTS_PAGE_ACCOUNTS_FLAG] },
    }),
    featureFlags: {
      subscribe: (name: string, _options: unknown, callback: (value: unknown) => void) => {
        if (name === PULL_REQUESTS_PAGE_ACCOUNTS_FLAG) flag.set = callback;
        return () => {};
      },
    },
  };
  /** Named reads on, `prUrl` read on this Mac as the host's account and, by name, as Bob. */
  const ready = async (prUrl: string, { asBob = true }: { asBob?: boolean } = {}) => {
    githubMacReadsPreference.write("on");
    setPageWriteOperationsForTests([...GITHUB_WRITE_SET_V1]);
    setReviewsFlagForTests(true);
    setMacWritesReleaseFlagForTests(true);
    namedReadsAvailable();
    await vi.waitFor(() => expect(flag.set).not.toBeNull());
    flag.set!(true);
    await vi.waitFor(() => expect(namedReadsAvailable()).toBe(true));
    await desktopReadTarget(prUrl);
    // The host's own account, as a session panel reads it, then Bob, as the page does.
    await readAsHost(prUrl);
    if (asBob) await readAsBob(prUrl);
  };
  /** One read of `prUrl` as gh's active account, the way a session panel reads it. */
  const readAsHost = async (prUrl: string) => {
    await readOnThisMac(macReadTargetAs(prUrl, null)!, "pull_request_overview", PR_PARAMS);
  };
  /** One read of `prUrl` by name as `reader`, the way the page's detail reads it. */
  const readNamed = async (prUrl: string, reader: MacNamedReader) => {
    await readOnThisMac(macReadTargetAs(prUrl, reader)!, "pull_request_overview", PR_PARAMS);
  };
  /** One read of `prUrl` as Bob. */
  const readAsBob = (prUrl: string) => readNamed(prUrl, BOB);
  /** The shell now answers as a newer or older one; the page asks it again. */
  const setWriteAs = async (next: boolean) => {
    state.writeAs = next;
    await refreshDesktopReadSupport();
  };
  /** The shell no longer reads or writes as a login by name, as a downgraded one doesn't. */
  const loseNamedSupport = async () => {
    state.readAs = false;
    state.writeAs = false;
    await refreshDesktopReadSupport();
  };
  /** `gh auth switch` to `login`: main's answers as gh's active account carry the next generation. */
  const switchActiveAccount = (login: string) => {
    state.hostLogin = login;
    state.hostGeneration += 1;
  };
  /** Bob signs in again: main's next answer as him carries `generation`. */
  const bobSignsInAgain = (generation: number) => {
    state.bobGeneration = generation;
    namedReaderChanged(BOB);
  };
  /**
   * Hold the next `operation` read until `release`; `started` settles once it was asked,
   * so a test can change who's signed in while it's in flight.
   */
  const pauseNextRead = (operation: string) => {
    let release!: () => void;
    paused.set(
      operation,
      new Promise<void>((resolve) => {
        release = resolve;
      }),
    );
    const started = vi.waitFor(() => expect(paused.has(operation)).toBe(false));
    return { started, release: () => release() };
  };
  return {
    codeHostQuery,
    codeHostWrite,
    codeHostWriteAs,
    flag,
    state,
    ready,
    readAsBob,
    readAsHost,
    readNamed,
    setWriteAs,
    loseNamedSupport,
    switchActiveAccount,
    bobSignsInAgain,
    pauseNextRead,
  };
}

/** Forget the shell. */
export function removeNamedWriteShell(): void {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
}
