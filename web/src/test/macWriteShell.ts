// A stand-in desktop shell that reads and writes GitHub as `mac-octo`, for tests of the
// panel's Mac write path with the real read-path and write-path stores.

import { expect, vi } from "vitest";

import {
  GITHUB_WRITE_SET_V1,
  setMacWritesReleaseFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import {
  acceptMacAnswer,
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  macReadTicket,
} from "@/lib/desktopReadPath";

export const MAC_IDENTITY = { host: "github.com", login: "mac-octo", generation: 1 };

/** What a shell that proves who read adds to an answer read as `viewer`. */
const provenanceOf = (viewer: string) => ({
  provenance: { version: 1 as const, provider: "github" as const, host: MAC_IDENTITY.host, viewer },
});

/** The panel read `host` as the Mac's account again, as it does after a switch. */
export function readAsMac({ proves = false }: { proves?: boolean } = {}): void {
  expect(
    acceptMacAnswer(macReadTicket("github", MAC_IDENTITY.host), {
      identity: MAC_IDENTITY,
      ...(proves ? provenanceOf(MAC_IDENTITY.login) : {}),
    }),
  ).toBe("accepted");
}

/**
 * Install the shell for `prUrl`. `codeHostWrite` answers each write; `reads` answers Mac
 * reads by operation, and `extraWrites` adds writes beyond set v1, such as the merge queue's.
 * A shell that `proves` names who read each answer, `viewer()` (the Mac's account by default).
 * The release flag is on; the page's write set is the caller's choice.
 */
export async function installMacWriteShell(
  prUrl: string,
  codeHostWrite: (request: unknown) => Promise<unknown>,
  reads: Record<string, unknown> = {},
  {
    extraWrites = [],
    proves = false,
    viewer = () => MAC_IDENTITY.login,
  }: { extraWrites?: readonly string[]; proves?: boolean; viewer?: () => string } = {},
) {
  const write = vi.fn(codeHostWrite);
  const cancel = vi.fn();
  const query = vi.fn(async (request: { operation: string }) =>
    request.operation in reads
      ? {
          ok: true,
          data: reads[request.operation],
          identity: MAC_IDENTITY,
          ...(proves ? provenanceOf(viewer()) : {}),
        }
      : { ok: true, data: [] },
  );
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: query,
    codeHostWrite: write,
    codeHostWriteCancel: cancel,
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
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
          ],
          accounts: true,
          ...(proves ? { answerProvenance: 1 } : {}),
          writes: { version: 1, operations: [...GITHUB_WRITE_SET_V1, ...extraWrites] },
        },
      ],
    }),
  };
  githubMacReadsPreference.write("on");
  await desktopReadTarget(prUrl);
  readAsMac({ proves });
  setMacWritesReleaseFlagForTests(true);
  return { write, cancel, query };
}

/** Forget the shell. */
export function removeMacWriteShell(): void {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
}
