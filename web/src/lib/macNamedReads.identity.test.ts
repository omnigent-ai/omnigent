// What a named login proved, or was refused as, belongs to one workspace user: another user
// signing in starts from checking.

import type * as IdentityModule from "@/lib/identity";
import { afterEach, expect, it, vi } from "vitest";

const { currentUser, identityListeners } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  identityListeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
}));

import { GITHUB_MAC_READ_OPERATIONS, resetDesktopReadPathForTests } from "./desktopReadPath";
import {
  acceptNamedAnswer,
  namedReaderRefused,
  namedReadsAvailable,
  namedReadTicket,
  namedRestoreProof,
  namedRestoreProofHolds,
  namedWriterIdentity,
  PULL_REQUESTS_PAGE_ACCOUNTS_FLAG,
  resetMacNamedReadsForTests,
} from "./macNamedReads";
import { resetMacReadScopesForTests } from "./macReadScope";

const READER = { host: "github.com", login: "octo" };

function switchUser(id: string | null) {
  currentUser.id = id;
  for (const listener of [...identityListeners]) listener();
}

/** A shell offering named reads, with the page's accounts flag turned on. */
async function namedReadsOn() {
  let setFlag: ((value: unknown) => void) | null = null;
  (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: async () => ({ ok: false }),
    codeHostAccounts: {
      get: async () => ({ ok: false }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
      onNamedChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS, "pull_request_inbox"],
          accounts: true,
          answerProvenance: 1,
          readAs: 1,
        },
      ],
      featureFlags: { names: [PULL_REQUESTS_PAGE_ACCOUNTS_FLAG] },
    }),
    featureFlags: {
      subscribe: (name: string, _options: unknown, callback: (value: unknown) => void) => {
        if (name === PULL_REQUESTS_PAGE_ACCOUNTS_FLAG) setFlag = callback;
        return () => {};
      },
    },
  };
  namedReadsAvailable();
  await vi.waitFor(() => expect(setFlag).not.toBeNull());
  setFlag!(true);
  await vi.waitFor(() => expect(namedReadsAvailable()).toBe(true));
}

const answerAs = (login: string) => ({
  identity: { host: "github.com", login, generation: 0, named: true },
  provenance: { version: 1, provider: "github", host: "github.com", viewer: login },
});

afterEach(() => {
  resetMacNamedReadsForTests();
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
  currentUser.id = "user-1";
});

it("never revives a restore proof when the user switches away and back", async () => {
  await namedReadsOn();
  expect(acceptNamedAnswer(namedReadTicket(READER), answerAs("octo"))).toBe("accepted");
  const proof = namedRestoreProof(READER, "octo")!;
  expect(namedRestoreProofHolds(READER, proof)).toBe(true);
  switchUser("user-2");
  switchUser("user-1");
  expect(namedRestoreProofHolds(READER, proof)).toBe(false);
  expect(namedRestoreProof(READER, "octo")).toBeNull();
});

it("forgets a refused login when another workspace user signs in, but not on the same user", () => {
  namedReadsAvailable();
  namedReaderRefused(READER);
  expect(namedWriterIdentity(READER)).toBe("refused");
  switchUser("user-1");
  expect(namedWriterIdentity(READER)).toBe("refused");
  switchUser("user-2");
  expect(namedWriterIdentity(READER)).toBe("checking");
});

it("stops hearing identity changes at a reset", () => {
  namedReadsAvailable();
  expect(identityListeners.size).toBe(1);
  resetMacNamedReadsForTests();
  expect(identityListeners.size).toBe(0);
});
