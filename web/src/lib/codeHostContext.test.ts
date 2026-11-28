import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const authenticatedFetch = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch }));

import {
  fetchCodeHostContext,
  isCurrentCodeHostContext,
  parseCodeHostContext,
  PENDING_RETRY_MS,
  readCodeHostContext,
  repositoryRemoteHosts,
  resetCodeHostContextReads,
  useCodeHostContext,
} from "./codeHostContext";
import { resetWorkspaceFastTrackForTests } from "./workspaceFastTrack";

const SESSION = "conv_1";
const HEAD = "a".repeat(40);
const URL = `/v1/sessions/${SESSION}/resources/environments/default/repository-context`;

function body(overrides: Record<string, unknown> = {}) {
  return {
    status: "ok",
    context_key: "key-a",
    branch: "feature",
    head_sha: HEAD,
    push: {
      remote_name: "fork",
      branch: "feature",
      host: "github.com",
      owner: "me",
      repo: "app",
      tracking_sha: HEAD,
    },
    push_unresolved: null,
    remotes: [
      { name: "upstream", host: "github.com", owner: "acme", repo: "app" },
      { name: "fork", host: "github.com", owner: "me", repo: "app" },
    ],
    unsupported_remote_count: 0,
    truncated_remotes: false,
    ...overrides,
  };
}

function installShell(bridge: Record<string, unknown> | undefined) {
  const target = window as unknown as Record<string, unknown>;
  if (bridge) target.omnigentDesktop = { kind: "electron", ...bridge };
  else delete target.omnigentDesktop;
}

/** A desktop shell whose workspace requests `answer`, listing `capabilities`. */
function shell(
  answer: (request: { url: string; requestId?: string }) => Promise<unknown>,
  capabilities = ["cancel_requests", "repository_context"],
) {
  const workspaceRequest = vi.fn(answer);
  const cancelWorkspaceRequest = vi.fn(async () => ({ ok: true }));
  installShell({
    workspaceRequest,
    cancelWorkspaceRequest,
    getDesktopFeatures: async () => ({
      workspaceFastTrack: true,
      workspaceFastTrackCapabilities: capabilities,
    }),
  });
  return { workspaceRequest, cancelWorkspaceRequest };
}

const served = (reply: unknown) => async () => ({
  ok: true,
  status: 200,
  body: reply,
  route: "workspace-helper",
});

function deferred<Value>() {
  let resolve: (value: Value) => void = () => {};
  const promise = new Promise<Value>((onResolve) => {
    resolve = onResolve;
  });
  return { promise, resolve };
}

afterEach(() => {
  installShell(undefined);
  resetWorkspaceFastTrackForTests();
  resetCodeHostContextReads();
  authenticatedFetch.mockReset();
});

describe("parseCodeHostContext", () => {
  it("keeps a well-formed context, push repository included", () => {
    expect(parseCodeHostContext(body())).toEqual(body());
  });

  it.each(["pending", "unsupported_config", "not_a_git_repo", "failed"])(
    "passes the %s status on",
    (status) => {
      expect(parseCodeHostContext({ status })).toEqual({ status });
    },
  );

  it.each([
    ["no answer", null],
    ["an unknown status", { status: "later" }],
    ["no key", body({ context_key: "" })],
    ["a malformed commit", body({ head_sha: "abc" })],
    ["a push without its repository", body({ push: { remote_name: "fork", branch: "f" } })],
    ["a malformed tracking commit", body({ push: { ...body().push, tracking_sha: "zz" } })],
    ["remotes that aren't a list", body({ remotes: "origin" })],
    ["a remote without a host", body({ remotes: [{ name: "origin", owner: "a", repo: "b" }] })],
  ])("treats %s as unsupported", (_label, answer) => {
    expect(parseCodeHostContext(answer)).toEqual({ status: "unsupported" });
  });

  it("names why there's no push target only when there isn't one", () => {
    expect(parseCodeHostContext(body({ push: null, push_unresolved: "push_url" }))).toMatchObject({
      push: null,
      push_unresolved: "push_url",
    });
    expect(parseCodeHostContext(body({ push_unresolved: "refspec" }))).toMatchObject({
      push_unresolved: null,
    });
  });
});

describe("repositoryRemoteHosts", () => {
  it("takes the push target's host when it pushes to that repository", () => {
    const context = parseCodeHostContext(
      body({
        push: { ...body().push, host: "GHE.example.com" },
        remotes: [{ name: "fork", host: "github.com", owner: "me", repo: "app" }],
      }),
    );
    expect(repositoryRemoteHosts(context, "Me/App")).toEqual({
      hosts: ["ghe.example.com"],
      complete: true,
    });
    // The push target names it even when the remote list was cut short.
    expect(
      repositoryRemoteHosts(
        parseCodeHostContext(
          body({ push: { ...body().push, host: "ghe.example.com" }, truncated_remotes: true }),
        ),
        "me/app",
      ),
    ).toEqual({ hosts: ["ghe.example.com"], complete: true });
  });

  it("falls back to every remote that names the repository, once per host", () => {
    const context = parseCodeHostContext(
      body({
        remotes: [
          { name: "upstream", host: "github.com", owner: "acme", repo: "app" },
          { name: "mirror", host: "GitHub.com", owner: "acme", repo: "app" },
          { name: "enterprise", host: "ghe.example.com", owner: "acme", repo: "app" },
          { name: "fork", host: "github.com", owner: "me", repo: "app" },
        ],
      }),
    );
    // The branch pushes to the fork, not acme/app, so the push target says nothing about it.
    expect(repositoryRemoteHosts(context, "acme/app")).toEqual({
      hosts: ["github.com", "ghe.example.com"],
      complete: true,
    });
  });

  it("says when the shell listed only some remotes", () => {
    const context = parseCodeHostContext(body({ push: null, truncated_remotes: true }));
    expect(repositoryRemoteHosts(context, "acme/app")).toEqual({
      hosts: ["github.com"],
      complete: false,
    });
  });

  it("names no host for a repository no remote names, and nothing without a context", () => {
    const context = parseCodeHostContext(body());
    expect(repositoryRemoteHosts(context, "other/repo")).toEqual({ hosts: [], complete: true });
    expect(repositoryRemoteHosts(undefined, "acme/app")).toBeNull();
    expect(repositoryRemoteHosts({ status: "unsupported" }, "acme/app")).toBeNull();
    expect(repositoryRemoteHosts(context, null)).toBeNull();
  });
});

describe("fetchCodeHostContext", () => {
  it("asks the shell for the session alone, desktop only and cancellable", async () => {
    const { workspaceRequest } = shell(served(body()));
    const signal = new AbortController().signal;
    expect(await fetchCodeHostContext(SESSION, { signal })).toEqual(body());
    expect(workspaceRequest).toHaveBeenCalledWith({
      url: URL,
      desktopOnly: true,
      requestId: expect.any(String),
    });
  });

  it.each([
    ["in a browser", () => installShell(undefined)],
    [
      "on a shell without the fast track (an Intel Mac, or the helper off)",
      () =>
        installShell({
          workspaceRequest: vi.fn(),
          getDesktopFeatures: async () => ({ workspaceFastTrack: false }),
        }),
    ],
    [
      "on a shell that predates repository context",
      () => shell(served(body()), ["cancel_requests"]),
    ],
  ])("is unsupported %s, without asking anything", async (_label, setup) => {
    setup();
    expect(await fetchCodeHostContext(SESSION)).toEqual({ status: "unsupported" });
    const bridge = (window as unknown as { omnigentDesktop?: { workspaceRequest?: unknown } })
      .omnigentDesktop;
    if (bridge?.workspaceRequest) expect(bridge.workspaceRequest).not.toHaveBeenCalled();
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it.each([
    ["verifying", { status: "pending" }],
    ["helper_outdated", { status: "unsupported" }],
    ["remote_route_off", { status: "unsupported" }],
    ["unknown_endpoint", { status: "unsupported" }],
  ])("maps the shell declining with %s, never falling back to HTTPS", async (reason, expected) => {
    shell(async () => ({ ok: false, reason }));
    expect(await fetchCodeHostContext(SESSION)).toEqual(expected);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("cancels the shell's work when the caller gives up, and rejects at once", async () => {
    const answering = deferred<unknown>();
    const { workspaceRequest, cancelWorkspaceRequest } = shell(() => answering.promise);
    const controller = new AbortController();
    const reading = fetchCodeHostContext(SESSION, { signal: controller.signal });
    await vi.waitFor(() => expect(workspaceRequest).toHaveBeenCalled());
    controller.abort();
    await expect(reading).rejects.toThrow();
    const { requestId } = workspaceRequest.mock.calls[0]![0];
    expect(cancelWorkspaceRequest).toHaveBeenCalledWith({ requestId });
  });

  it("asks nothing when the caller already gave up", async () => {
    const { workspaceRequest } = shell(served(body()));
    const controller = new AbortController();
    controller.abort();
    await expect(fetchCodeHostContext(SESSION, { signal: controller.signal })).rejects.toThrow();
    expect(workspaceRequest).not.toHaveBeenCalled();
  });

  it("asks nothing when the caller gives up while the shell's features load", async () => {
    let loaded: (features: unknown) => void = () => {};
    const workspaceRequest = vi.fn(served(body()));
    installShell({
      workspaceRequest,
      getDesktopFeatures: () =>
        new Promise((resolve) => {
          loaded = resolve;
        }),
    });
    const controller = new AbortController();
    const reading = fetchCodeHostContext(SESSION, { signal: controller.signal });
    controller.abort();
    loaded({ workspaceFastTrack: true, workspaceFastTrackCapabilities: ["repository_context"] });
    await expect(reading).rejects.toThrow();
    expect(workspaceRequest).not.toHaveBeenCalled();
  });
});

describe("readCodeHostContext", () => {
  it("accepts a newer read's changed context, as after a commit or branch switch", async () => {
    let reply = body();
    shell(async () => ({ ok: true, status: 200, body: reply, route: "workspace-helper" }));
    expect(await readCodeHostContext(SESSION)).toMatchObject({ context_key: "key-a" });
    expect(isCurrentCodeHostContext(SESSION, "key-a")).toBe(true);
    reply = body({ context_key: "key-b", head_sha: "b".repeat(40) });
    expect(await readCodeHostContext(SESSION)).toMatchObject({ context_key: "key-b" });
    expect(isCurrentCodeHostContext(SESSION, "key-a")).toBe(false);
    expect(isCurrentCodeHostContext(SESSION, "key-b")).toBe(true);
  });

  it("never lets an overtaken read's late answer replace the newer one", async () => {
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    const answers = [first, second];
    shell(() => answers.shift()!.promise);
    const reading = readCodeHostContext(SESSION);
    await vi.waitFor(() => expect(answers).toHaveLength(1));
    const rereading = readCodeHostContext(SESSION);
    await vi.waitFor(() => expect(answers).toHaveLength(0));
    second.resolve({
      ok: true,
      status: 200,
      body: body({ context_key: "key-b" }),
      route: "workspace-helper",
    });
    expect(await rereading).toMatchObject({ context_key: "key-b" });
    first.resolve({
      ok: true,
      status: 200,
      body: body({ context_key: "key-a" }),
      route: "workspace-helper",
    });
    // The overtaken read gives back the newer context, not its own.
    expect(await reading).toMatchObject({ context_key: "key-b" });
    expect(isCurrentCodeHostContext(SESSION, "key-b")).toBe(true);
    expect(isCurrentCodeHostContext(SESSION, "key-a")).toBe(false);
  });

  it.each([
    ["a shell that can cancel", ["cancel_requests", "repository_context"]],
    ["a shell that can't cancel", ["repository_context"]],
  ])(
    "never accepts an answer that arrives after the read was aborted, on %s",
    async (_label, capabilities) => {
      const answering = deferred<unknown>();
      const { workspaceRequest } = shell(() => answering.promise, capabilities);
      const controller = new AbortController();
      const reading = readCodeHostContext(SESSION, { signal: controller.signal });
      await vi.waitFor(() => expect(workspaceRequest).toHaveBeenCalled());
      controller.abort();
      await expect(reading).rejects.toThrow();
      // The shell answers anyway; nothing takes its answer.
      answering.resolve({
        ok: true,
        status: 200,
        body: body({ context_key: "key-late" }),
        route: "workspace-helper",
      });
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
      expect(isCurrentCodeHostContext(SESSION, "key-late")).toBe(false);
    },
  );

  it("keeps each session's context to itself", async () => {
    const answering = deferred<unknown>();
    shell(async (request) =>
      request.url.includes("conv_1")
        ? answering.promise
        : {
            ok: true,
            status: 200,
            body: body({ context_key: "key-other" }),
            route: "workspace-helper",
          },
    );
    const first = readCodeHostContext("conv_1");
    // The user switches sessions while the first session's read is in flight.
    expect(await readCodeHostContext("conv_2")).toMatchObject({ context_key: "key-other" });
    answering.resolve({
      ok: true,
      status: 200,
      body: body({ context_key: "key-a" }),
      route: "workspace-helper",
    });
    expect(await first).toMatchObject({ context_key: "key-a" });
    expect(isCurrentCodeHostContext("conv_2", "key-a")).toBe(false);
    expect(isCurrentCodeHostContext("conv_2", "key-other")).toBe(true);
  });
});

describe("useCodeHostContext", () => {
  it("cancels the old session's read on a switch, and never shows its answer", async () => {
    const first = deferred<unknown>();
    const { workspaceRequest, cancelWorkspaceRequest } = shell(async (request) =>
      request.url.includes("conv_1")
        ? first.promise
        : {
            ok: true,
            status: 200,
            body: body({ context_key: "key-two" }),
            route: "workspace-helper",
          },
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    const { result, rerender } = renderHook(({ session }) => useCodeHostContext(session), {
      wrapper,
      initialProps: { session: "conv_1" },
    });
    await vi.waitFor(() => expect(workspaceRequest).toHaveBeenCalledTimes(1));
    rerender({ session: "conv_2" });
    await vi.waitFor(() => expect(result.current.data).toMatchObject({ context_key: "key-two" }));
    // Nothing observes the first session any more, so its read is cancelled in main.
    const { requestId } = workspaceRequest.mock.calls[0]![0];
    await vi.waitFor(() => expect(cancelWorkspaceRequest).toHaveBeenCalledWith({ requestId }));
    first.resolve({
      ok: true,
      status: 200,
      body: body({ context_key: "key-one" }),
      route: "workspace-helper",
    });
    await vi.waitFor(() => expect(result.current.data).toMatchObject({ context_key: "key-two" }));
  });

  it("asks again while the helper is still starting, and stops once it answers", async () => {
    let calls = 0;
    const { workspaceRequest } = shell(async () =>
      ++calls === 1 ? { ok: false, reason: "verifying" } : served(body())(),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    const { result, unmount } = renderHook(() => useCodeHostContext(SESSION), { wrapper });
    try {
      await vi.waitFor(() => expect(result.current.data).toEqual({ status: "pending" }));
      await vi.waitFor(() => expect(result.current.data?.status).toBe("ok"), {
        timeout: PENDING_RETRY_MS * 2 + 500,
      });
      await new Promise((resolve) => {
        setTimeout(resolve, PENDING_RETRY_MS + 200);
      });
      expect(workspaceRequest).toHaveBeenCalledTimes(2);
    } finally {
      unmount();
      client.clear();
    }
  });
});
