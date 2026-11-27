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

import {
  namedReaderRefused,
  namedReadsAvailable,
  namedWriterIdentity,
  resetMacNamedReadsForTests,
} from "./macNamedReads";

const READER = { host: "github.com", login: "octo" };

function switchUser(id: string | null) {
  currentUser.id = id;
  for (const listener of [...identityListeners]) listener();
}

afterEach(() => {
  resetMacNamedReadsForTests();
  currentUser.id = "user-1";
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
