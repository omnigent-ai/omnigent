import { afterEach, describe, expect, it, vi } from "vitest";

import {
  desktopCodeHostQuery,
  githubFastTrackQuery,
  workspaceFastTrackRequest,
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
});

describe("githubFastTrackQuery", () => {
  it("returns a capable shell's answer as it is", async () => {
    installShell({ githubQuery: vi.fn(async () => ({ ok: true, data: [] })) });
    expect(await githubFastTrackQuery("pull_request_details", ACCOUNT, {})).toEqual({
      ok: true,
      data: [],
    });
  });

  it.each(MALFORMED)("answers unsupported for a malformed answer %j", async (answer) => {
    installShell({ githubQuery: vi.fn(async () => answer) });
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
  const request = { sessionId: "conv_1", method: "GET", path: "/files" } as unknown as Parameters<
    typeof workspaceFastTrackRequest
  >[0];

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
      installShell({ workspaceRequest: vi.fn(async () => answer) });
      // oxlint-disable-next-line no-await-in-loop -- each shell in turn
      expect(await workspaceFastTrackRequest(request)).toEqual({
        ok: true,
        status: 200,
        bodyText,
        route: "workspace-helper",
        ignoredFiles: "hidden",
      });
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
