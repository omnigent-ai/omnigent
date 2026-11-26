import { afterEach, describe, expect, it, vi } from "vitest";

import {
  authorizeDesktopCodeHostRun,
  desktopCodeHostQuery,
  desktopCodeHostWrite,
  githubFastTrackQuery,
  onWorkspaceAccessReady,
  reportPullRequestCommentWrites,
  reportPullRequestDialog,
  reportPullRequestTiming,
  type WorkspaceFastTrackRequest,
  workspaceFastTrackRequest,
  workspaceHostKeyReview,
} from "./nativeBridgeCodeHost";

function installShell(bridge: Record<string, unknown> | undefined) {
  const target = window as unknown as Record<string, unknown>;
  if (bridge) target.omnigentDesktop = { kind: "electron", ...bridge };
  else delete target.omnigentDesktop;
}

afterEach(() => installShell(undefined));

const REQUEST = { provider: "github", host: "github.com", operation: "pull_request_details" };
const ACCOUNT = { host: "github.com", login: "octo" };

// What a partial or mismatched shell might answer instead of a result.
const MALFORMED = [
  { ok: false },
  { ok: false, error: null },
  { ok: false, error: "boom" },
  { ok: false, error: { message: "no code" } },
  { ok: false, error: { code: "not_found" } },
  { ok: false, error: { code: "not_found", message: 7 } },
  { ok: true },
  { ok: true, data: undefined },
  { ok: true, data: () => {} },
  { ok: false, error: { code: "rate_limited", message: "Slow down.", retryAt: "soon" } },
  { ok: false, error: { code: "search_rejected", message: "No.", detail: 7 } },
  { ok: "yes", data: {} },
  { ok: 1, error: { code: "query_failed", message: "x" } },
];

describe("desktopCodeHostQuery", () => {
  it("returns a capable shell's answer and failure as they are", async () => {
    installShell({ codeHostQuery: vi.fn(async () => ({ ok: true, data: { n: 1 } })) });
    expect(await desktopCodeHostQuery(REQUEST)).toEqual({ ok: true, data: { n: 1 } });
    const full = {
      ok: true,
      data: null,
      identity: { host: "github.com", login: "octo", generation: 2 },
      budget: { resource: "graphql", limit: 5000, remaining: 10, resetAt: 1 },
    };
    installShell({ codeHostQuery: vi.fn(async () => full) });
    expect(await desktopCodeHostQuery(REQUEST)).toEqual(full);
    const limited = {
      ok: false,
      error: {
        code: "rate_limited",
        message: "Slow down.",
        retryAt: 5,
        limit: { kind: "primary", resource: "graphql" },
      },
    };
    installShell({ codeHostQuery: vi.fn(async () => limited) });
    expect(await desktopCodeHostQuery(REQUEST)).toEqual(limited);
    const failure = { ok: false, error: { code: "not_found", message: "Gone." } };
    installShell({ codeHostQuery: vi.fn(async () => failure) });
    expect(await desktopCodeHostQuery(REQUEST)).toEqual(failure);
  });

  it("keeps a newer shell's answers: an unknown error code, limit kind or unreadable budget", async () => {
    const newer = { ok: false, error: { code: "held_by_policy", message: "Ask an admin." } };
    installShell({ codeHostQuery: vi.fn(async () => newer) });
    expect(await desktopCodeHostQuery(REQUEST)).toEqual(newer);
    const limit = { kind: "abuse", resource: "graphql" };
    const limited = { ok: false, error: { code: "rate_limited", message: "Slow down.", limit } };
    installShell({ codeHostQuery: vi.fn(async () => limited) });
    expect(await desktopCodeHostQuery(REQUEST)).toMatchObject({
      error: { limit: { kind: "unknown", resource: "graphql" } },
    });
    const budget = { resource: "search", limit: 30, remaining: 1, resetAt: 1 };
    installShell({ codeHostQuery: vi.fn(async () => ({ ok: true, data: [], budget })) });
    expect(await desktopCodeHostQuery(REQUEST)).toEqual({ ok: true, data: [] });
  });

  it.each([
    { identity: "octo" },
    { identity: { host: "github.com", login: 7, generation: 1 } },
    { identity: { host: "github.com", login: "octo", generation: Number.NaN } },
    { provenance: { version: 1, provider: "github", host: "github.com" } },
    { githubReader: { login: "octo", generation: 1, named: false } },
  ])(
    "leaves out an unreadable field, for the reader that needs it to refuse: %j",
    async (field) => {
      installShell({ codeHostQuery: vi.fn(async () => ({ ok: true, data: [], ...field })) });
      expect(await desktopCodeHostQuery(REQUEST)).toEqual({ ok: true, data: [] });
    },
  );

  it("answers unsupported without a shell, or on an older one without the bridge", async () => {
    installShell(undefined);
    expect(await desktopCodeHostQuery(REQUEST)).toMatchObject({
      ok: false,
      error: { code: "unsupported" },
    });
    installShell({});
    expect(await desktopCodeHostQuery(REQUEST)).toMatchObject({
      ok: false,
      error: { code: "unsupported" },
    });
  });

  it.each(MALFORMED)("answers unsupported for a malformed answer %j", async (answer) => {
    installShell({ codeHostQuery: vi.fn(async () => answer) });
    const result = await desktopCodeHostQuery(REQUEST);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe("unsupported");
  });

  it("never sends a read aborted already", async () => {
    const query = vi.fn(async () => ({ ok: true, data: {} }));
    installShell({ codeHostQuery: query });
    const aborted = new AbortController();
    aborted.abort();
    expect(await desktopCodeHostQuery(REQUEST, aborted.signal)).toMatchObject({
      ok: false,
      error: { code: "cancelled" },
    });
    expect(query).not.toHaveBeenCalled();
  });

  it("lets go of a read aborted in flight by its readId, and only while it's in flight", async () => {
    let answer!: (value: unknown) => void;
    const cancel = vi.fn(async () => ({ ok: true }));
    installShell({
      codeHostQuery: vi.fn(
        () =>
          new Promise((resolve) => {
            answer = resolve;
          }),
      ),
      codeHostQueryCancel: cancel,
    });
    const controller = new AbortController();
    const pending = desktopCodeHostQuery({ ...REQUEST, readId: "read_1" }, controller.signal);
    controller.abort();
    await vi.waitFor(() => expect(cancel).toHaveBeenCalledWith({ readId: "read_1" }));
    answer({ ok: true, data: {} });
    await pending;

    const settled = new AbortController();
    const second = desktopCodeHostQuery({ ...REQUEST, readId: "read_2" }, settled.signal);
    answer({ ok: true, data: {} });
    await second;
    settled.abort();
    await Promise.resolve();
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("has nothing to let go of for a read without a readId", async () => {
    let answer!: (value: unknown) => void;
    const cancel = vi.fn(async () => ({ ok: true }));
    installShell({
      codeHostQuery: vi.fn(
        () =>
          new Promise((resolve) => {
            answer = resolve;
          }),
      ),
      codeHostQueryCancel: cancel,
    });
    const controller = new AbortController();
    const pending = desktopCodeHostQuery(REQUEST, controller.signal);
    controller.abort();
    answer({ ok: true, data: {} });
    await pending;
    expect(cancel).not.toHaveBeenCalled();
  });
});

describe("desktopCodeHostWrite", () => {
  const write = {
    requestId: "write_1",
    provider: "github",
    host: "github.com",
    actor: { login: "octo", generation: 1 },
    operation: "start_review",
    params: {},
  };

  it("keeps a successful several-step write's steps, with the review it started", async () => {
    const steps = {
      completed: [{ step: "start_review", review_id: "PRR_1" }],
      not_attempted: [{ step: "submit_review", index: 1 }],
    };
    installShell({
      codeHostWrite: vi.fn(async () => ({ ok: true, data: null, actor: { login: "octo" }, steps })),
    });
    expect(await desktopCodeHostWrite(write)).toEqual({
      ok: true,
      data: null,
      actor: { login: "octo" },
      steps,
    });
  });

  it("leaves out steps it can't read, and keeps the write", async () => {
    installShell({
      codeHostWrite: vi.fn(async () => ({
        ok: true,
        data: null,
        actor: { login: "octo" },
        steps: { completed: "all" },
      })),
    });
    expect(await desktopCodeHostWrite(write)).toEqual({
      ok: true,
      data: null,
      actor: { login: "octo" },
    });
  });
});

describe("desktopCodeHostWrite's failures", () => {
  const write = {
    requestId: "write_1",
    provider: "github",
    host: "github.com",
    actor: { login: "octo", generation: 1 },
    operation: "merge",
    params: {},
  };

  it("answers unsupported, sending nothing, from a shell without the bridge", async () => {
    installShell({});
    expect(await desktopCodeHostWrite(write)).toMatchObject({
      ok: false,
      error: { code: "unsupported" },
    });
  });

  // Once sent, GitHub may have made the change, so the page must check before trying again.
  it.each([
    ["a throw", () => Promise.reject(new Error("gone"))],
    ["no answer", async () => null],
    ["a success without its actor", async () => ({ ok: true, data: null })],
    ["a failure without its code", async () => ({ ok: false, error: { message: "x" } })],
  ])("reads %s from a sent write as an unknown outcome", async (_name, codeHostWrite) => {
    installShell({ codeHostWrite: vi.fn(codeHostWrite) });
    expect(await desktopCodeHostWrite(write)).toMatchObject({
      ok: false,
      error: { code: "outcome_unknown" },
    });
  });
});

describe("authorizeDesktopCodeHostRun", () => {
  const run = {
    requestId: "run_1",
    provider: "github",
    host: "github.com",
    actor: { login: "octo", generation: 1 },
    entries: [{ operation: "merge", params: {} }],
  };

  it("passes a grant the shell gives, and refuses an empty or missing one", async () => {
    const authorize = vi.fn(async () => ({ ok: true, grantId: "grant_1" }));
    installShell({ codeHostWriteAuthorizeRun: authorize });
    expect(await authorizeDesktopCodeHostRun(run)).toEqual({ ok: true, grantId: "grant_1" });
    for (const answer of [{ ok: true, grantId: "" }, { ok: true }, null]) {
      authorize.mockResolvedValueOnce(answer as never);
      // oxlint-disable-next-line no-await-in-loop -- each answer in turn
      expect(await authorizeDesktopCodeHostRun(run)).toEqual({ ok: false, code: "unsupported" });
    }
  });

  it("never authorizes a named login's run as the default account", async () => {
    const asDefault = vi.fn(async () => ({ ok: true, grantId: "grant_default" }));
    installShell({ codeHostWriteAuthorizeRun: asDefault });
    expect(await authorizeDesktopCodeHostRun({ ...run, macLogin: "bob" })).toEqual({
      ok: false,
      code: "unsupported",
    });
    expect(asDefault).not.toHaveBeenCalled();
  });
});

describe("workspaceHostKeyReview", () => {
  const OLD = `SHA256:${"a".repeat(43)}`;
  const NEW = `SHA256:${"b".repeat(43)}`;
  const review = {
    ok: true,
    kind: "changed",
    token: "t",
    oldFingerprint: OLD,
    newFingerprint: NEW,
    machine: "builder",
    server: "build.example.com",
  };

  it("returns a well-formed review, including one with no old key", async () => {
    installShell({ workspaceHostKeyReview: vi.fn(async () => review) });
    expect(await workspaceHostKeyReview("conv_1")).toEqual(review);
    const first = { ...review, kind: "store_invalid", oldFingerprint: null };
    installShell({ workspaceHostKeyReview: vi.fn(async () => first) });
    expect(await workspaceHostKeyReview("conv_1")).toEqual(first);
  });

  it.each([
    ["a new fingerprint of the wrong shape", { newFingerprint: "SHA256:short" }],
    ["an MD5 new fingerprint", { newFingerprint: `MD5:${"b".repeat(43)}` }],
    ["a missing new fingerprint", { newFingerprint: undefined }],
    ["an old fingerprint of the wrong shape", { oldFingerprint: "SHA256:short" }],
    ["an empty machine", { machine: "" }],
    ["an unknown kind", { kind: "added" }],
  ])("refuses a review with %s", async (_name, change) => {
    installShell({ workspaceHostKeyReview: vi.fn(async () => ({ ...review, ...change })) });
    expect(await workspaceHostKeyReview("conv_1")).toEqual({ ok: false, reason: "unsupported" });
  });
});

describe("diagnostics reports", () => {
  const reporters = [
    ["reportPullRequestTiming", () => reportPullRequestTiming({} as never)],
    ["reportPullRequestDialog", () => reportPullRequestDialog({ dialog: "d", outcome: "shown" })],
    [
      "reportPullRequestCommentWrites",
      () => reportPullRequestCommentWrites({ reason: "r", outcome: "shown" }),
    ],
  ] as const;

  it.each(reporters)("%s hands the record to the shell", (name, report) => {
    const send = vi.fn();
    installShell({ [name]: send });
    report();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it.each(reporters)("%s ignores an older shell, a throw and a rejection", async (name, report) => {
    installShell({});
    expect(report).not.toThrow();
    installShell({
      [name]: () => {
        throw new Error("boom");
      },
    });
    expect(report).not.toThrow();
    const rejection = Promise.reject(new Error("boom"));
    installShell({ [name]: () => rejection });
    expect(report).not.toThrow();
    // A rejection left unhandled would fail the run.
    await rejection.catch(() => {});
  });
});

describe("onWorkspaceAccessReady", () => {
  it("gives a no-op unsubscribe when the shell returns something else", () => {
    installShell({ onWorkspaceAccessReady: vi.fn(() => "subscribed") });
    const unsubscribe = onWorkspaceAccessReady(() => {});
    expect(() => unsubscribe()).not.toThrow();
  });

  it("returns the shell's own unsubscribe", () => {
    const shellUnsubscribe = vi.fn();
    installShell({ onWorkspaceAccessReady: vi.fn(() => shellUnsubscribe) });
    onWorkspaceAccessReady(() => {})();
    expect(shellUnsubscribe).toHaveBeenCalledTimes(1);
  });
});

describe("githubFastTrackQuery", () => {
  it("returns a capable shell's answer as it is", async () => {
    installShell({ githubQuery: vi.fn(async () => ({ ok: true, data: [] })) });
    expect(await githubFastTrackQuery("pull_request_details", ACCOUNT, {})).toEqual({
      ok: true,
      data: [],
    });
  });

  // The parser's whole matrix runs through desktopCodeHostQuery above.
  it("answers unsupported for a malformed answer", async () => {
    installShell({ githubQuery: vi.fn(async () => ({ ok: true })) });
    const result = await githubFastTrackQuery("pull_request_details", ACCOUNT, {});
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe("unsupported");
  });
});

function cyclic() {
  const listing: Record<string, unknown> = {};
  listing.self = listing;
  return listing;
}

describe("workspaceFastTrackRequest", () => {
  const request: WorkspaceFastTrackRequest = { url: "/v1/sessions/conv_1/filesystem/src" };

  it("returns a capable shell's answer and refusal as they are", async () => {
    for (const [body, bodyText] of [
      [{ files: [] }, '{"files":[]}'],
      [null, "null"],
      ["text", '"text"'],
    ]) {
      const answer = {
        ok: true,
        status: 200,
        body,
        route: "workspace-helper",
        ignoredFiles: "hidden",
      };
      const send = vi.fn(async () => answer);
      installShell({ workspaceRequest: send });
      // oxlint-disable-next-line no-await-in-loop -- each shell in turn
      expect(await workspaceFastTrackRequest(request)).toEqual({
        ok: true,
        status: 200,
        bodyText,
        route: "workspace-helper",
        ignoredFiles: "hidden",
      });
      expect(send).toHaveBeenCalledWith(request);
    }
    installShell({ workspaceRequest: vi.fn(async () => ({ ok: false, reason: "offline" })) });
    expect(await workspaceFastTrackRequest(request)).toEqual({ ok: false, reason: "offline" });
  });

  it.each([
    { ok: false },
    { ok: false, reason: 7 },
    { ok: "yes" },
    { ok: true },
    { ok: true, status: 200, body: {} },
    { ok: true, status: 200, route: "workspace-helper" },
    { ok: true, status: 500, body: {}, route: "workspace-helper" },
    { ok: true, status: 200, body: undefined, route: "workspace-helper" },
    { ok: true, status: 200, body: () => {}, route: "workspace-helper" },
    { ok: true, status: 200, body: {}, route: "workspace-helper", ignoredFiles: "some" },
    { ok: true, status: 200, body: { count: 1n }, route: "workspace-helper" },
    { ok: true, status: 200, body: cyclic(), route: "workspace-helper" },
    { ok: false, reason: null },
  ])("answers unsupported for malformed answer #%#", async (answer) => {
    installShell({ workspaceRequest: vi.fn(async () => answer) });
    expect(await workspaceFastTrackRequest(request)).toEqual({
      ok: false,
      reason: "unsupported",
    });
  });
});
