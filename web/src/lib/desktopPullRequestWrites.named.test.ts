// Changes to a PR the Pull Requests page reads as one of this Mac's logins by name: made as that
// login, with its own generation, through `codeHostWriteAs`, or not at all. A shell without the
// method or the capability blocks them; it never makes them as the host's account instead.

import { afterEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
}));

import {
  beginMacWrite,
  macWriteBlockedMessage,
  macRouteCurrent,
  macWriteDispatch,
  macWriteRoute,
  macWriteRouteStill,
  MacWriteError,
  resetDesktopPullRequestWritesForTests,
  sameMacRoute,
  sendMacWrite,
  setMacWritesReleaseFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { fetchDesktopLabelOptions } from "@/lib/desktopPullRequests";
import { macReadTargetAs, resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import {
  namedReaderRefused,
  namedWriterIdentity,
  resetMacNamedReadsForTests,
} from "@/lib/macNamedReads";
import { macNamedReadScopeKey, resetMacReadScopesForTests } from "@/lib/macReadScope";
import {
  BOB,
  HOST_LOGIN,
  installNamedWriteShell,
  removeNamedWriteShell,
} from "@/test/macNamedWriteShell";

const PR = "https://github.com/acme/app/pull/42";
const COMMENT = {
  ok: true,
  data: { comment: { author: "bob", body: "hi", created_at: null, url: `${PR}#c1` } },
  actor: { login: "bob" },
};
const parseAny = (data: unknown) => data;
const ALICE = { host: "github.com", login: HOST_LOGIN };

afterEach(() => {
  setMacWritesReleaseFlagForTests(null);
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe("the route for a PR the page reads as its own login", () => {
  it("acts as that login, with the generation its last kept answer proved", async () => {
    const shell = installNamedWriteShell(() => COMMENT, { bobGeneration: 4 });
    await shell.ready(PR);
    const route = macWriteRoute(PR, BOB);
    expect(route?.actor).toEqual({ login: "bob", generation: 4 });
    expect(route?.reader).toEqual(BOB);
    expect(route?.target.readAs).toBe("bob");
    // Its answers are kept in the named login's own scope.
    expect(beginMacWrite(route!).scope).toEqual(
      macNamedReadScopeKey("github.com", "bob", "user-1"),
    );
  });

  it("sends through codeHostWriteAs naming the login, and the actor as main parses it", async () => {
    const shell = installNamedWriteShell(() => COMMENT);
    await shell.ready(PR);
    const ticket = beginMacWrite(macWriteRoute(PR, BOB)!);
    await sendMacWrite(
      ticket,
      "pull_request_comment",
      { repository: "acme/app", number: 42, body: "hi" },
      parseAny,
    );
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    expect(shell.codeHostWriteAs).toHaveBeenCalledTimes(1);
    const [request] = shell.codeHostWriteAs.mock.calls[0]!;
    expect(request).toMatchObject({
      host: "github.com",
      macLogin: "bob",
      operation: "pull_request_comment",
    });
    expect(request.actor).toEqual({ login: "bob", generation: 0 });
  });

  it("writes as the host's account, through the legacy method, outside the page", async () => {
    const shell = installNamedWriteShell(() => ({ ...COMMENT, actor: { login: HOST_LOGIN } }));
    await shell.ready(PR);
    const route = macWriteRoute(PR, null);
    expect(route?.actor.login).toBe(HOST_LOGIN);
    expect(route?.reader).toBeNull();
    await sendMacWrite(
      beginMacWrite(route!),
      "pull_request_comment",
      { repository: "acme/app", number: 42, body: "hi" },
      parseAny,
    );
    expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
    const [request] = shell.codeHostWrite.mock.calls[0]!;
    expect(request).not.toHaveProperty("macLogin");
    expect(Object.keys(request.actor as object).sort()).toEqual(["generation", "login"]);
  });

  it("tells a named login apart from the same login as gh's active account, at equal generations", async () => {
    // Named Alice and host Alice both at generation 1: one account, two independent scopes.
    const shell = installNamedWriteShell(() => COMMENT, { bobGeneration: 1 });
    await shell.ready(PR);
    await shell.readNamed(PR, ALICE);
    const named = macWriteRoute(PR, ALICE)!;
    const host = macWriteRoute(PR, null)!;
    expect(named.actor).toEqual(host.actor);
    expect(sameMacRoute(named, host)).toBe(false);
    expect(sameMacRoute(named, macWriteRoute(PR, ALICE))).toBe(true);
    // Once the shell stops writing by name, the named route isn't current, nor its change sent.
    await shell.setWriteAs(false);
    expect(macRouteCurrent(PR, named)).toBeNull();
    const still = macWriteRouteStill(PR, named);
    expect(still instanceof MacWriteError && still.code).toBe("blocked");
  });

  it("uses the host's account where the page reads another host by name", async () => {
    const shell = installNamedWriteShell();
    await shell.ready(PR);
    const route = macWriteRoute(PR, { host: "ghe.example.com", login: "carol" });
    expect(route?.actor.login).toBe(HOST_LOGIN);
    expect(macWriteDispatch(PR, { host: "ghe.example.com", login: "carol" }).kind).toBe("mac");
  });
});

describe("a change the page's own login can't make now is blocked, never sent elsewhere", () => {
  it.each([
    ["an older shell without writeAs", { writeAs: false }],
    ["a partial bridge without codeHostWriteAs", { writeAsMethod: false }],
  ])("blocks on %s, asking for an update", async (_label, options) => {
    const shell = installNamedWriteShell(() => COMMENT, options);
    await shell.ready(PR);
    const dispatch = macWriteDispatch(PR, BOB);
    expect(dispatch.kind).toBe("blocked");
    if (dispatch.kind !== "blocked") return;
    expect(dispatch.unavailable.reason).toBe("named_writes_need_update");
    expect(macWriteBlockedMessage(dispatch.unavailable)).toBe(
      "Update the app to make changes as @bob here. Nothing was changed.",
    );
    expect(macWriteRoute(PR, BOB)).toBeNull();
    // The host's own account still writes outside the page on the same shell.
    expect(macWriteDispatch(PR, null).kind).toBe("mac");
  });

  it("on an older shell, blocks a pick of gh's active account by name too, before and after a switch", async () => {
    const shell = installNamedWriteShell(() => COMMENT, { writeAs: false });
    await shell.ready(PR);
    const before = macWriteDispatch(PR, ALICE);
    expect(before.kind === "blocked" && before.unavailable.reason).toBe("named_writes_need_update");
    shell.switchActiveAccount("carol");
    await shell.readAsHost(PR);
    const after = macWriteDispatch(PR, ALICE);
    expect(after.kind === "blocked" && after.unavailable.reader).toEqual(ALICE);
    // The host's own account still writes outside the page, as whoever gh has active now.
    expect(macWriteRoute(PR, null)?.actor).toEqual({ login: "carol", generation: 2 });
  });

  it.each([true, false])(
    "blocks, never sends as the host, an operation the shell doesn't offer (writeAs %s)",
    async (writeAs) => {
      const shell = installNamedWriteShell(() => COMMENT, { writeAs });
      await shell.ready(PR);
      for (const reader of [ALICE, BOB]) {
        expect(macWriteDispatch(PR, reader, () => false).kind).toBe("blocked");
      }
      expect(macWriteDispatch(PR, null, () => false).kind).toBe("host");
    },
  );

  it("waits for the login's first proven read, and blocks once main refused it", async () => {
    const shell = installNamedWriteShell();
    await shell.ready(PR, { asBob: false });
    const checking = macWriteDispatch(PR, BOB);
    expect(checking.kind === "blocked" && checking.unavailable.reason).toBe("account_checking");
    namedReaderRefused(BOB);
    const refused = macWriteDispatch(PR, BOB);
    expect(refused.kind === "blocked" && refused.unavailable.reason).toBe("no_account");
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
  });

  it("blocks once the login signed out, and acts as its new generation after it's back", async () => {
    const shell = installNamedWriteShell(() => COMMENT);
    await shell.ready(PR);
    shell.state.bobSignedIn = false;
    await expect(shell.readAsBob(PR)).rejects.toBeTruthy();
    const refused = macWriteDispatch(PR, BOB);
    expect(refused.kind === "blocked" && refused.unavailable.reason).toBe("no_account");
    shell.state.bobSignedIn = true;
    shell.state.bobGeneration = 2;
    await shell.readAsBob(PR);
    expect(macWriteRoute(PR, BOB)?.actor).toEqual({ login: "bob", generation: 2 });
  });

  it("blocks a write the shell stopped offering after the control rendered", async () => {
    const shell = installNamedWriteShell(() => COMMENT);
    await shell.ready(PR);
    const rendered = macWriteRoute(PR, BOB);
    expect(rendered).not.toBeNull();
    await shell.setWriteAs(false);
    expect(macWriteDispatch(PR, BOB).kind).toBe("blocked");
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  });

  it("treats a named login's account_changed as its own change, not the host's", async () => {
    const shell = installNamedWriteShell(() => ({
      ok: false,
      error: { code: "account_changed", message: "changed" },
    }));
    await shell.ready(PR);
    const ticket = beginMacWrite(macWriteRoute(PR, BOB)!);
    await expect(
      sendMacWrite(
        ticket,
        "pull_request_comment",
        { repository: "acme/app", number: 42, body: "hi" },
        parseAny,
      ),
    ).rejects.toBeInstanceOf(MacWriteError);
    // Bob's proven identity is dropped until a read as him again; the host's account still acts.
    expect(namedWriterIdentity(BOB)).toBe("checking");
    expect(macWriteRoute(PR, null)?.actor.login).toBe(HOST_LOGIN);
  });
});

describe("the reads a change as the page's own login needs", () => {
  const optionReads = (shell: ReturnType<typeof installNamedWriteShell>) =>
    shell.codeHostQuery.mock.calls
      .map(([request]) => request)
      .filter((request) => request.operation === "pull_request_label_options");

  it("read as that login where the shell writes as it", async () => {
    const shell = installNamedWriteShell();
    await shell.ready(PR);
    await fetchDesktopLabelOptions(macWriteRoute(PR, BOB)!.target);
    expect(optionReads(shell)).toEqual([expect.objectContaining({ macLogin: "bob" })]);
  });

  it("stay with the host's account on a shell that doesn't", async () => {
    const shell = installNamedWriteShell(undefined, { writeAs: false });
    await shell.ready(PR);
    await fetchDesktopLabelOptions(macReadTargetAs(PR, BOB)!);
    const [request] = optionReads(shell);
    expect(request).not.toHaveProperty("macLogin");
  });
});
