import type * as IdentityModule from "@/lib/identity";
import { afterEach, describe, expect, it, vi } from "vitest";

const { currentUser } = vi.hoisted(() => ({ currentUser: { id: "user-1" as string | null } }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
}));

import { DesktopPullRequestError, readOnThisMac } from "./desktopPullRequests";
import {
  GITHUB_MAC_READ_OPERATIONS,
  macOffers,
  resetDesktopReadPathForTests,
} from "./desktopReadPath";
import {
  acceptNamedAnswer,
  leaveNamedReader,
  namedReaderChanged,
  namedReaderRefused,
  namedRestoreProof,
  namedRestoreProofHolds,
  namedWriterIdentity,
  namedReadsAvailable,
  namedReadTicket,
  PULL_REQUESTS_PAGE_ACCOUNTS_FLAG,
  resetMacNamedReadsForTests,
} from "./macNamedReads";
import {
  macNamedReadScopeKey,
  macReadScopeKey,
  onMacReadScopeAdvanced,
  resetMacReadScopesForTests,
  type MacReadScopeKey,
} from "./macReadScope";

const BOB = { host: "github.com", login: "bob" };
const TARGET = { provider: "github" as const, host: "github.com", readAs: "bob" };

/** A whole named answer as main sends it, read as `login`. */
const namedAnswer = (data: unknown, login = "bob", generation = 0) => ({
  ok: true,
  data,
  identity: { host: "github.com", login, generation, named: true },
  provenance: { version: 1, provider: "github", host: "github.com", viewer: login },
});

function installShell(bridge: Record<string, unknown> | undefined) {
  const target = window as unknown as Record<string, unknown>;
  if (bridge) target.omnigentDesktop = { kind: "electron", ...bridge };
  else delete target.omnigentDesktop;
}

/** A shell offering named reads behind the flag; returns its controls. */
/** `readAs: null` leaves the capability out, as an older shell does. */
function namedShell({ readAs = 1 as number | null } = {}) {
  const flag: { set: ((value: unknown) => void) | null } = { set: null };
  const named: { emit: ((change: unknown) => void) | null } = { emit: null };
  let answer: (request: Record<string, unknown>) => unknown = (request) =>
    request.macLogin
      ? namedAnswer({ ok: 1 })
      : {
          ok: true,
          data: { ok: 1 },
          identity: { host: "github.com", login: "alice", generation: 1 },
          provenance: { version: 1, provider: "github", host: "github.com", viewer: "alice" },
        };
  const codeHostQuery = vi.fn(async (request: Record<string, unknown>) => answer(request));
  installShell({
    codeHostQuery,
    codeHostAccounts: {
      get: async () => ({ ok: false }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
      onNamedChanged: (callback: (change: unknown) => void) => {
        named.emit = callback;
        return () => {};
      },
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [
            ...GITHUB_MAC_READ_OPERATIONS,
            "pull_request_inbox",
            "pull_request_label_options",
          ],
          accounts: true,
          answerProvenance: 1,
          ...(readAs !== null ? { readAs } : {}),
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
  });
  return {
    codeHostQuery,
    flag,
    named,
    answerWith: (next: typeof answer) => {
      answer = next;
    },
  };
}

/** Turn the flag on and wait until named reads are available. */
async function ready(shell: ReturnType<typeof namedShell>) {
  namedReadsAvailable();
  await vi.waitFor(() => expect(shell.flag.set).not.toBeNull());
  shell.flag.set!(true);
  await vi.waitFor(() => expect(namedReadsAvailable()).toBe(true));
}

const advanced: MacReadScopeKey[] = [];
const stopWatching = onMacReadScopeAdvanced((key) => {
  advanced.push(key);
});

afterEach(() => {
  advanced.length = 0;
  resetMacNamedReadsForTests();
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
  installShell(undefined);
  currentUser.id = "user-1";
});
void stopWatching;

describe("reading as a named login", () => {
  it("names the login only for the reads main allows by name", async () => {
    const shell = namedShell();
    await ready(shell);
    await readOnThisMac(TARGET, "pull_request_inbox", {});
    await readOnThisMac(TARGET, "pull_request_label_options", { repository: "o/r", number: 1 });
    const [inbox, labels] = shell.codeHostQuery.mock.calls.map(([request]) => request);
    expect(inbox).toMatchObject({
      account: "mac",
      macLogin: "bob",
      operation: "pull_request_inbox",
    });
    // Write options stay with the host's own account.
    expect(labels).toMatchObject({ account: "mac" });
    expect(labels).not.toHaveProperty("macLogin");
  });

  it("refuses a named read the shell doesn’t offer, asking nothing", async () => {
    const shell = namedShell({ readAs: null });
    namedReadsAvailable();
    await vi.waitFor(() => expect(shell.flag.set).not.toBeNull());
    shell.flag.set!(true);
    // Support has settled (it lists the operations), and still offers no named reads.
    await vi.waitFor(() => expect(macOffers("github", "pull_request_inbox")).toBe(true));
    expect(namedReadsAvailable()).toBe(false);
    await expect(readOnThisMac(TARGET, "pull_request_inbox", {})).rejects.toMatchObject({
      code: "unsupported",
    });
    expect(shell.codeHostQuery).not.toHaveBeenCalled();
  });

  it.each([
    ["no identity", { ok: true, data: 1 }],
    [
      "an identity that isn't named",
      { ...namedAnswer(1), identity: { host: "github.com", login: "bob", generation: 0 } },
    ],
    ["another login", namedAnswer(1, "alice")],
    [
      "a NaN generation",
      {
        ...namedAnswer(1),
        identity: { host: "github.com", login: "bob", generation: Number.NaN, named: true },
      },
    ],
    [
      "a missing host",
      { ...namedAnswer(1), identity: { login: "bob", generation: 0, named: true } },
    ],
    [
      "a login that isn't text",
      { ...namedAnswer(1), identity: { host: "github.com", login: 7, generation: 0, named: true } },
    ],
    ["no provenance", { ...namedAnswer(1), provenance: undefined }],
    [
      "a viewer missing",
      { ...namedAnswer(1), provenance: { version: 1, provider: "github", host: "github.com" } },
    ],
    [
      "another viewer",
      {
        ...namedAnswer(1),
        provenance: { version: 1, provider: "github", host: "github.com", viewer: "alice" },
      },
    ],
    [
      "another host",
      {
        ...namedAnswer(1),
        provenance: { version: 1, provider: "github", host: "ghe.example.com", viewer: "bob" },
      },
    ],
  ])("refuses an answer with %s, without throwing anything else", async (_label, envelope) => {
    const shell = namedShell();
    await ready(shell);
    shell.answerWith(() => envelope);
    const read = readOnThisMac(TARGET, "pull_request_inbox", {});
    await expect(read).rejects.toBeInstanceOf(DesktopPullRequestError);
    await expect(read).rejects.toMatchObject({ code: "query_failed" });
  });

  it("drops an answer when named reads go away mid-read, and doesn't ask again", async () => {
    const shell = namedShell();
    await ready(shell);
    let release: (() => void) | null = null;
    shell.answerWith(
      () =>
        new Promise((resolve) => {
          release = () => resolve(namedAnswer({ secret: 1 }));
        }),
    );
    const read = readOnThisMac(TARGET, "pull_request_inbox", {});
    await vi.waitFor(() => expect(release).not.toBeNull());
    shell.flag.set!(false); // rolled back while reading
    release!();
    await expect(read).rejects.toMatchObject({ code: "unsupported" });
    expect(shell.codeHostQuery).toHaveBeenCalledTimes(1);
    expect(advanced).toContainEqual(macNamedReadScopeKey("github.com", "bob"));
  });

  it("refuses a read sent before the page left its reader, even when it comes back", async () => {
    const shell = namedShell();
    await ready(shell);
    const first = namedReadTicket(BOB);
    leaveNamedReader(BOB); // to another account…
    const again = namedReadTicket(BOB); // …and back
    expect(acceptNamedAnswer(first, namedAnswer(1))).toBe("moved");
    expect(acceptNamedAnswer(again, namedAnswer(1))).toBe("accepted");
  });

  it("drops the login's reads when it signs out, leaving the host's account's alone", async () => {
    const shell = namedShell();
    await ready(shell);
    const before = namedReadTicket(BOB);
    shell.named.emit!({ host: "github.com", login: "bob", generation: 1 });
    expect(advanced).toEqual([macNamedReadScopeKey("github.com", "bob")]);
    expect(advanced).not.toContainEqual(macReadScopeKey("github", "github.com"));
    expect(acceptNamedAnswer(before, namedAnswer(1, "bob", 1))).toBe("moved");
    // An answer from before the sign-out's generation is refused too.
    expect(acceptNamedAnswer(namedReadTicket(BOB), namedAnswer(1, "bob", 0))).toBe("moved");
    expect(acceptNamedAnswer(namedReadTicket(BOB), namedAnswer(1, "bob", 1))).toBe("accepted");
  });

  it("never keeps another workspace user's answer", async () => {
    const shell = namedShell();
    await ready(shell);
    const ticket = namedReadTicket(BOB);
    currentUser.id = "user-2";
    expect(acceptNamedAnswer(ticket, namedAnswer(1))).toBe("moved");
  });

  it("leaves the login as `left`, so what this device kept for it may stay", async () => {
    const shell = namedShell();
    await ready(shell);
    const moves: unknown[] = [];
    const stop = onMacReadScopeAdvanced((key, move) => moves.push([key, move]));
    leaveNamedReader(BOB);
    namedReaderRefused(BOB);
    stop();
    expect(moves).toEqual([
      [macNamedReadScopeKey("github.com", "bob"), "left"],
      [macNamedReadScopeKey("github.com", "bob"), "refused"],
    ]);
  });
});

describe("restoring an answer kept as a named login", () => {
  it("needs a fresh answer this run proving that exact login, whatever its case", async () => {
    const shell = namedShell();
    await ready(shell);
    expect(namedRestoreProof(BOB, "bob")).toBeNull();
    expect(acceptNamedAnswer(namedReadTicket(BOB), namedAnswer(1))).toBe("accepted");
    const proof = namedRestoreProof(BOB, "Bob");
    expect(proof).toEqual({ epoch: expect.any(Number), userId: "user-1" });
    expect(namedRestoreProofHolds(BOB, proof!)).toBe(true);
    // Another login's viewer, or another login's scope, proves nothing.
    expect(namedRestoreProof(BOB, "carol")).toBeNull();
    expect(namedRestoreProof({ host: "github.com", login: "carol" }, "carol")).toBeNull();
  });

  it.each([
    ["the page leaves the login", () => leaveNamedReader(BOB)],
    ["main refuses it", () => namedReaderRefused(BOB)],
    ["it changes", () => namedReaderChanged(BOB)],
    ["another workspace user signs in", () => (currentUser.id = "user-2")],
  ])("stops holding once %s, and gives no new proof without a fresh answer", async (_, move) => {
    const shell = namedShell();
    await ready(shell);
    acceptNamedAnswer(namedReadTicket(BOB), namedAnswer(1));
    const proof = namedRestoreProof(BOB, "bob")!;
    move();
    expect(namedRestoreProofHolds(BOB, proof)).toBe(false);
    expect(namedRestoreProof(BOB, "bob")).toBeNull();
  });

  it("never makes a writer or clears a refusal itself", async () => {
    const shell = namedShell();
    await ready(shell);
    namedReaderRefused(BOB);
    expect(namedRestoreProof(BOB, "bob")).toBeNull();
    expect(namedWriterIdentity(BOB)).toBe("refused");
  });
});
