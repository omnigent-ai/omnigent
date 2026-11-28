import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const authenticatedFetch = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch }));

import { setSessionHost } from "./sessionHost";
import { writeShowIgnoredFiles } from "./ignoredFilesPreference";
import {
  answersLocally,
  NotAnsweredByDesktopError,
  NotAnsweredLocallyError,
  refetchWhenWorkspaceAccessReady,
  resetWorkspaceFastTrackForTests,
  shellFiltersIgnoredFiles,
  shellReportsSessionRoutes,
  streamWorkspaceSearch,
  WORKSPACE_IGNORED_FILES_HEADER,
  WORKSPACE_ROUTE_HEADER,
  workspaceFetch,
} from "./workspaceFastTrack";

const SESSION = "conv_local";
const HOST = "host_mac";
const LISTING_URL = `/v1/sessions/${SESSION}/resources/environments/default/filesystem/src?limit=1000&order=asc`;
const LISTING = { object: "list", base: "/Users/me/project/src", data: [], has_more: false };

function installShell(bridge: Record<string, unknown> | undefined) {
  const target = window as unknown as Record<string, unknown>;
  if (bridge) target.omnigentDesktop = { kind: "electron", ...bridge };
  else delete target.omnigentDesktop;
}

function capableShell(workspaceRequest: (request: unknown) => Promise<unknown>) {
  installShell({
    workspaceRequest: vi.fn(workspaceRequest),
    getDesktopFeatures: async () => ({ workspaceFastTrack: true }),
  });
  return (window as unknown as { omnigentDesktop: { workspaceRequest: ReturnType<typeof vi.fn> } })
    .omnigentDesktop.workspaceRequest;
}

const answered = async () => ({ ok: true, status: 200, body: LISTING, route: "workspace-helper" });

beforeEach(() => {
  authenticatedFetch.mockResolvedValue(new Response("{}", { status: 200 }));
  setSessionHost(SESSION, HOST);
});

afterEach(() => {
  writeShowIgnoredFiles(false);
  installShell(undefined);
  resetWorkspaceFastTrackForTests();
  setSessionHost(SESSION, null);
  authenticatedFetch.mockReset();
});

describe("workspaceFetch", () => {
  it("answers from the shell when it serves a local session's request", async () => {
    const workspaceRequest = capableShell(answered);

    const response = await workspaceFetch(LISTING_URL);

    // The shell gets the URL and the ignored-files choice; it asks the server what
    // the session may read. Ignored files are hidden until the user shows them.
    expect(workspaceRequest).toHaveBeenCalledWith({
      url: LISTING_URL,
      hideIgnored: true,
      directoryChains: false,
    });
    expect(response.ok).toBe(true);
    expect(response.headers.get(WORKSPACE_ROUTE_HEADER)).toBe("workspace-helper");
    expect(await response.json()).toEqual(LISTING);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("follows the user's choice to show git-ignored files", async () => {
    const workspaceRequest = capableShell(async () => ({
      ok: true,
      status: 200,
      body: LISTING,
      route: "workspace-helper",
      ignoredFiles: "shown",
    }));
    writeShowIgnoredFiles(true);
    const response = await workspaceFetch(LISTING_URL);
    expect(workspaceRequest).toHaveBeenCalledWith({
      url: LISTING_URL,
      hideIgnored: false,
      directoryChains: false,
    });
    expect(response.headers.get(WORKSPACE_IGNORED_FILES_HEADER)).toBe("shown");
  });

  it("reports hidden ignored files only when the shell says it left them out", async () => {
    capableShell(async () => ({ ...(await answered()), ignoredFiles: "hidden" }));
    const hidden = await workspaceFetch(LISTING_URL);
    expect(hidden.headers.get(WORKSPACE_IGNORED_FILES_HEADER)).toBe("hidden");
    resetWorkspaceFastTrackForTests();
    // A shell that predates the choice lists everything.
    capableShell(answered);
    const older = await workspaceFetch(LISTING_URL);
    expect(older.headers.get(WORKSPACE_IGNORED_FILES_HEADER)).toBe("shown");
  });

  it("asks for every file when a caller checks whether a path exists", async () => {
    const workspaceRequest = capableShell(answered);
    await workspaceFetch(LISTING_URL, {}, { followIgnoredFilesPreference: false });
    expect(workspaceRequest).toHaveBeenCalledWith({
      url: LISTING_URL,
      hideIgnored: false,
      directoryChains: false,
    });
  });

  it("asks the shell for directory chains only when the caller does", async () => {
    const workspaceRequest = capableShell(answered);
    await workspaceFetch(LISTING_URL, {}, { directoryChains: true });
    expect(workspaceRequest).toHaveBeenCalledWith({
      url: LISTING_URL,
      hideIgnored: true,
      directoryChains: true,
    });
  });

  it("takes HTTPS in the browser", async () => {
    await workspaceFetch(LISTING_URL);
    expect(authenticatedFetch).toHaveBeenCalledWith(LISTING_URL, {});
  });

  it("takes HTTPS in an older shell without the bridge method", async () => {
    installShell({ getDesktopFeatures: async () => ({ workspaceFastTrack: true }) });
    await workspaceFetch(LISTING_URL);
    expect(authenticatedFetch).toHaveBeenCalledOnce();
  });

  it.each([
    ["no features", undefined],
    ["null features", async () => null],
    ["features without the field", async () => ({})],
    ["the feature off", async () => ({ workspaceFastTrack: false })],
  ])("takes HTTPS for a shell reporting %s", async (_label, getDesktopFeatures) => {
    const workspaceRequest = vi.fn(answered);
    installShell({ workspaceRequest, ...(getDesktopFeatures ? { getDesktopFeatures } : {}) });
    await workspaceFetch(LISTING_URL);
    expect(workspaceRequest).not.toHaveBeenCalled();
    expect(authenticatedFetch).toHaveBeenCalledOnce();
  });

  it.each([
    ["a remote host", { ok: false, reason: "remote_host" }],
    ["a session it hasn't verified yet", { ok: false, reason: "verifying" }],
    ["a sandboxed environment", { ok: false, reason: "confined_environment" }],
    ["a crashed helper", { ok: false, reason: "helper_unavailable" }],
    ["a malformed answer", "nonsense"],
  ])("takes HTTPS when the shell declines for %s", async (_label, answer) => {
    capableShell(async () => answer);
    const signal = new AbortController().signal;
    await workspaceFetch(LISTING_URL, { signal });
    expect(authenticatedFetch).toHaveBeenCalledWith(LISTING_URL, { signal });
  });

  it.each([
    ["no body", undefined],
    ["a BigInt", { count: 1n }],
  ])("takes HTTPS for an answer whose body JSON can't carry: %s", async (_label, body) => {
    capableShell(async () => ({ ok: true, status: 200, body, route: "workspace-helper" }));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const response = await workspaceFetch(LISTING_URL);
    expect(authenticatedFetch).toHaveBeenCalledOnce();
    expect(response).toBe(await vi.mocked(authenticatedFetch).mock.results[0]?.value);
  });

  it("takes HTTPS when the bridge throws", async () => {
    capableShell(async () => {
      throw new Error("ipc gone");
    });
    vi.spyOn(console, "warn").mockImplementation(() => {});
    await workspaceFetch(LISTING_URL);
    expect(authenticatedFetch).toHaveBeenCalledOnce();
  });

  it("takes HTTPS for a session with no host", async () => {
    const workspaceRequest = capableShell(answered);
    setSessionHost(SESSION, null);
    await workspaceFetch(LISTING_URL);
    expect(workspaceRequest).not.toHaveBeenCalled();
    expect(authenticatedFetch).toHaveBeenCalledOnce();
  });

  it.each([
    ["the changes feed", `/v1/sessions/${SESSION}/resources/environments/default/changes`],
    ["another session resource", `/v1/sessions/${SESSION}/messages`],
  ])("never promotes %s", async (_label, url) => {
    const workspaceRequest = capableShell(answered);
    await workspaceFetch(url);
    expect(workspaceRequest).not.toHaveBeenCalled();
  });

  it("never promotes a write", async () => {
    const workspaceRequest = capableShell(answered);
    await workspaceFetch(LISTING_URL, { method: "PUT", body: "{}" });
    expect(workspaceRequest).not.toHaveBeenCalled();
  });

  it("rejects as aborted when the caller gave up during a fast-track read", async () => {
    const controller = new AbortController();
    capableShell(async () => {
      controller.abort();
      return answered();
    });
    await expect(workspaceFetch(LISTING_URL, { signal: controller.signal })).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("shellFiltersIgnoredFiles", () => {
  it.each([
    ["a browser", undefined, false],
    ["a first fast-track shell that lists no capabilities", { workspaceFastTrack: true }, false],
    [
      "a shell with the fast track off",
      { workspaceFastTrack: false, workspaceFastTrackCapabilities: ["ignored_files"] },
      false,
    ],
    [
      "a shell listing other capabilities",
      { workspaceFastTrack: true, workspaceFastTrackCapabilities: ["something_else"] },
      false,
    ],
    [
      "a shell that filters ignored files",
      { workspaceFastTrack: true, workspaceFastTrackCapabilities: ["ignored_files"] },
      true,
    ],
  ])("is %s → %s", async (_label, features, expected) => {
    if (features) {
      installShell({ workspaceRequest: vi.fn(answered), getDesktopFeatures: async () => features });
    }
    expect(await shellFiltersIgnoredFiles()).toBe(expected);
  });

  it("still lets a first fast-track shell answer, reporting ignored files as shown", async () => {
    capableShell(answered);
    const response = await workspaceFetch(LISTING_URL);
    expect(response.headers.get(WORKSPACE_IGNORED_FILES_HEADER)).toBe("shown");
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("refetchWhenWorkspaceAccessReady", () => {
  function verifyingShell() {
    let verified = false;
    const listeners: ((sessionId: string) => void)[] = [];
    installShell({
      workspaceRequest: vi.fn(async (request: { hideIgnored?: boolean }) =>
        verified
          ? {
              ok: true,
              status: 200,
              body: LISTING,
              route: "workspace-helper",
              ignoredFiles: request.hideIgnored ? "hidden" : "shown",
            }
          : { ok: false, reason: "verifying" },
      ),
      onWorkspaceAccessReady: (listener: (sessionId: string) => void) => {
        listeners.push(listener);
        return () => listeners.splice(listeners.indexOf(listener), 1);
      },
      getDesktopFeatures: async () => ({
        workspaceFastTrack: true,
        workspaceFastTrackCapabilities: ["ignored_files"],
      }),
    });
    return {
      grant: (sessionId: string) => {
        verified = true;
        for (const listener of [...listeners]) listener(sessionId);
      },
      listeners,
    };
  }

  async function routeOf(client: QueryClient, queryKey: unknown[]) {
    return client.fetchQuery({
      queryKey,
      queryFn: async () => (await workspaceFetch(LISTING_URL)).headers.get(WORKSPACE_ROUTE_HEADER),
      staleTime: Infinity,
    });
  }

  it("replaces the first load's HTTPS answer once this Mac may answer", async () => {
    const shell = verifyingShell();
    const client = new QueryClient();
    const unsubscribe = refetchWhenWorkspaceAccessReady(client);
    const treeKey = ["workspace-all-files", SESSION, "", "ignored:hidden"];
    const otherSessionKey = ["workspace-all-files", "conv_other", "", "ignored:hidden"];
    const unrelatedKey = ["workspace-environment", SESSION];
    // Verification is still running, so the first load takes HTTPS, unfiltered.
    expect(await routeOf(client, treeKey)).toBeNull();
    await routeOf(client, otherSessionKey);
    await routeOf(client, unrelatedKey);
    expect(authenticatedFetch).toHaveBeenCalledTimes(3);

    shell.grant(SESSION);
    await vi.waitFor(() => expect(client.getQueryState(treeKey)?.isInvalidated).toBe(true));
    // The invalidated tree now loads from this Mac, with ignored files hidden.
    expect(await routeOf(client, treeKey)).toBe("workspace-helper");
    expect(client.getQueryState(otherSessionKey)?.isInvalidated).toBe(false);
    expect(client.getQueryState(unrelatedKey)?.isInvalidated).toBe(false);

    unsubscribe();
    expect(shell.listeners).toHaveLength(0);
  });

  it("asks again for a repository context the helper answered while still verifying", async () => {
    const shell = verifyingShell();
    const client = new QueryClient();
    const unsubscribe = refetchWhenWorkspaceAccessReady(client);
    const contextKey = ["code-host-context", SESSION];
    client.setQueryData(contextKey, { status: "pending" });
    shell.grant(SESSION);
    await vi.waitFor(() => expect(client.getQueryState(contextKey)?.isInvalidated).toBe(true));
    unsubscribe();
  });

  it("asks this Mac again even while the first HTTPS load is still running", async () => {
    const shell = verifyingShell();
    let releaseHttps!: () => void;
    authenticatedFetch.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          releaseHttps = () => resolve(new Response("{}", { status: 200 }));
        }),
    );
    const client = new QueryClient();
    refetchWhenWorkspaceAccessReady(client);
    const treeKey = ["workspace-all-files", SESSION, "", "ignored:hidden"];
    const observer = new QueryObserver(client, {
      queryKey: treeKey,
      queryFn: async () => (await workspaceFetch(LISTING_URL)).headers.get(WORKSPACE_ROUTE_HEADER),
      staleTime: Infinity,
    });
    const unsubscribe = observer.subscribe(() => {});
    await vi.waitFor(() => expect(authenticatedFetch).toHaveBeenCalledOnce());

    // Access is granted while the unfiltered HTTPS answer is still on its way.
    shell.grant(SESSION);
    await vi.waitFor(() => expect(client.getQueryData(treeKey)).toBe("workspace-helper"));
    releaseHttps();
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(client.getQueryData(treeKey)).toBe("workspace-helper");
    unsubscribe();
  });

  it("keeps the HTTPS answer for a session this Mac never answers", async () => {
    verifyingShell();
    const client = new QueryClient();
    refetchWhenWorkspaceAccessReady(client);
    const treeKey = ["workspace-all-files", SESSION, "", "ignored:hidden"];
    expect(await routeOf(client, treeKey)).toBeNull();
    // No notice comes, so the tree isn't refetched or emptied.
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(client.getQueryState(treeKey)?.isInvalidated).toBe(false);
    expect(client.getQueryData(treeKey)).toBeNull();
  });

  it("does nothing under a shell that can't send the notice", () => {
    installShell({ workspaceRequest: vi.fn(answered) });
    expect(() => refetchWhenWorkspaceAccessReady(new QueryClient())()).not.toThrow();
  });
});

describe("answersLocally", () => {
  it("follows the route that answered the session's latest file request", async () => {
    let declines = false;
    capableShell(async () => (declines ? { ok: false, reason: "verifying" } : answered()));
    expect(answersLocally(SESSION)).toBe(false);
    await workspaceFetch(LISTING_URL);
    expect(answersLocally(SESSION)).toBe(true);
    declines = true;
    await workspaceFetch(LISTING_URL);
    // This request took HTTPS.
    expect(answersLocally(SESSION)).toBe(false);
  });

  it("is never true for a session that isn't on a host", async () => {
    capableShell(answered);
    setSessionHost(SESSION, null);
    await workspaceFetch(LISTING_URL);
    expect(answersLocally(SESSION)).toBe(false);
  });
});

describe("workspaceFetch, local only", () => {
  it("answers from this Mac, and rejects rather than taking HTTPS when it can't", async () => {
    let declines = false;
    capableShell(async () => (declines ? { ok: false, reason: "verifying" } : answered()));
    const local = await workspaceFetch(LISTING_URL, {}, { localOnly: true });
    expect(local.headers.get(WORKSPACE_ROUTE_HEADER)).toBe("workspace-helper");
    declines = true;
    await expect(workspaceFetch(LISTING_URL, {}, { localOnly: true })).rejects.toBeInstanceOf(
      NotAnsweredLocallyError,
    );
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("rejects in a browser, where nothing is answered on this Mac", async () => {
    await expect(workspaceFetch(LISTING_URL, {}, { localOnly: true })).rejects.toBeInstanceOf(
      NotAnsweredLocallyError,
    );
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("workspaceFetch, local only, against another machine's answers", () => {
  const overSsh = async () => ({
    ok: true,
    status: 200,
    body: LISTING,
    route: "workspace-helper-ssh",
  });

  it("asks the shell for this Mac only, and only for speculative loads", async () => {
    const workspaceRequest = capableShell(answered);
    await workspaceFetch(LISTING_URL, {}, { localOnly: true });
    expect(workspaceRequest).toHaveBeenLastCalledWith(expect.objectContaining({ localOnly: true }));
    await workspaceFetch(LISTING_URL);
    expect(workspaceRequest.mock.lastCall?.[0]).not.toHaveProperty("localOnly");
  });

  it("rejects another machine's answer, or one naming no route, without keeping it or taking HTTPS", async () => {
    // A shell that ignores localOnly, as one from before it can.
    for (const answer of [overSsh, async () => ({ ok: true, status: 200, body: LISTING })]) {
      capableShell(answered);
      // oxlint-disable-next-line no-await-in-loop -- each shell in turn
      await workspaceFetch(LISTING_URL);
      expect(answersLocally(SESSION)).toBe(true);
      capableShell(answer);
      // oxlint-disable-next-line no-await-in-loop -- each shell in turn
      await expect(workspaceFetch(LISTING_URL, {}, { localOnly: true })).rejects.toBeInstanceOf(
        NotAnsweredLocallyError,
      );
      expect(answersLocally(SESSION), "no longer cheap to prefetch").toBe(false);
    }
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("rejects a held prefetch whose session moved to another machine while it waited", async () => {
    let release: (value: unknown) => void = () => {};
    capableShell(answered);
    await workspaceFetch(LISTING_URL);
    const held = capableShell(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    const prefetch = workspaceFetch(LISTING_URL, {}, { localOnly: true });
    await vi.waitFor(() => expect(held).toHaveBeenCalled());
    release(await overSsh());
    await expect(prefetch).rejects.toBeInstanceOf(NotAnsweredLocallyError);
    expect(answersLocally(SESSION)).toBe(false);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("still serves reads the user asked for from another machine, or from an old shell", async () => {
    capableShell(overSsh);
    const remote = await workspaceFetch(LISTING_URL);
    expect(remote.headers.get(WORKSPACE_ROUTE_HEADER)).toBe("workspace-helper-ssh");
    expect(answersLocally(SESSION)).toBe(false);
    capableShell(async () => ({ ok: true, status: 200, body: LISTING, route: "workspace-helper" }));
    const local = await workspaceFetch(LISTING_URL);
    expect(await local.json()).toEqual(LISTING);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("workspaceFetch, desktop only", () => {
  const overSsh = async () => ({
    ok: true,
    status: 200,
    body: LISTING,
    route: "workspace-helper-ssh",
  });

  it("keeps an answer from this Mac or over SSH, and never asks for this Mac only", async () => {
    for (const answer of [answered, overSsh]) {
      const workspaceRequest = capableShell(answer);
      // oxlint-disable-next-line no-await-in-loop -- each route in turn
      const response = await workspaceFetch(LISTING_URL, {}, { desktopOnly: true });
      expect(response.ok).toBe(true);
      expect(workspaceRequest.mock.lastCall?.[0]).not.toHaveProperty("localOnly");
      // So main records that a decline sends nothing to the server.
      expect(workspaceRequest.mock.lastCall?.[0]).toHaveProperty("desktopOnly", true);
    }
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("rejects with the shell's reason rather than taking HTTPS", async () => {
    capableShell(async () => ({ ok: false, reason: "remote_route_off" }));
    const declined = await workspaceFetch(LISTING_URL, {}, { desktopOnly: true }).catch(
      (error: unknown) => error,
    );
    expect(declined).toBeInstanceOf(NotAnsweredByDesktopError);
    expect((declined as NotAnsweredByDesktopError).reason).toBe("remote_route_off");
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("rejects in a browser, or for a session with no host, without taking HTTPS", async () => {
    await expect(workspaceFetch(LISTING_URL, {}, { desktopOnly: true })).rejects.toMatchObject({
      reason: "unsupported",
    });
    capableShell(answered);
    setSessionHost(SESSION, null);
    await expect(workspaceFetch(LISTING_URL, {}, { desktopOnly: true })).rejects.toBeInstanceOf(
      NotAnsweredByDesktopError,
    );
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("leaves a prefetch this Mac only, even when it is also desktop only", async () => {
    capableShell(overSsh);
    await expect(
      workspaceFetch(LISTING_URL, {}, { localOnly: true, desktopOnly: true }),
    ).rejects.toBeInstanceOf(NotAnsweredLocallyError);
  });
});

describe("shellReportsSessionRoutes", () => {
  const features = (capabilities: string[]) => async () => ({
    workspaceFastTrack: true,
    workspaceFastTrackCapabilities: capabilities,
  });

  it("needs both the session-route capability and the bridge method", async () => {
    const cases: [Record<string, unknown> | undefined, boolean][] = [
      [
        {
          workspaceRequest: vi.fn(),
          workspaceSessionRoute: vi.fn(),
          getDesktopFeatures: features(["session_route"]),
        },
        true,
      ],
      // A shell listing the capability without the method, or the method without it.
      [{ workspaceRequest: vi.fn(), getDesktopFeatures: features(["session_route"]) }, false],
      [
        {
          workspaceRequest: vi.fn(),
          workspaceSessionRoute: vi.fn(),
          getDesktopFeatures: features([]),
        },
        false,
      ],
      // An older shell that lists no capabilities at all.
      [
        {
          workspaceRequest: vi.fn(),
          workspaceSessionRoute: vi.fn(),
          getDesktopFeatures: async () => ({ workspaceFastTrack: true }),
        },
        false,
      ],
      [undefined, false],
    ];
    for (const [bridge, expected] of cases) {
      installShell(bridge);
      resetWorkspaceFastTrackForTests();
      // oxlint-disable-next-line no-await-in-loop -- each shell in turn
      expect(await shellReportsSessionRoutes(), JSON.stringify(Object.keys(bridge ?? {}))).toBe(
        expected,
      );
    }
  });
});

describe("workspaceFetch, branch-point comparisons", () => {
  const CHANGES_URL = `/v1/sessions/${SESSION}/resources/environments/default/changes?comparison=branch-point`;
  const HEAD_CHANGES_URL = `/v1/sessions/${SESSION}/resources/environments/default/changes`;
  const DIFF_URL = `/v1/sessions/${SESSION}/resources/environments/default/diff/src/a.ts`;

  function comparingShell(
    capabilities: string[],
    {
      cancel = true,
      workspaceRequest = vi.fn(answered) as ReturnType<typeof vi.fn>,
    }: { cancel?: boolean; workspaceRequest?: ReturnType<typeof vi.fn> } = {},
  ) {
    const cancelWorkspaceRequest = vi.fn(async () => ({ ok: true }));
    installShell({
      workspaceRequest,
      ...(cancel ? { cancelWorkspaceRequest } : {}),
      getDesktopFeatures: async () => ({
        workspaceFastTrack: true,
        workspaceFastTrackCapabilities: capabilities,
      }),
    });
    return { workspaceRequest, cancelWorkspaceRequest };
  }

  it("asks the shell for changes and diffs only when the request is desktop only", async () => {
    const { workspaceRequest } = comparingShell(["cancel_requests"]);
    await workspaceFetch(HEAD_CHANGES_URL);
    await workspaceFetch(DIFF_URL);
    expect(workspaceRequest).not.toHaveBeenCalled();
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);

    await workspaceFetch(CHANGES_URL, {}, { desktopOnly: true });
    expect(workspaceRequest).toHaveBeenCalledTimes(1);
    expect(workspaceRequest.mock.lastCall?.[0]).toMatchObject({
      url: CHANGES_URL,
      desktopOnly: true,
    });
  });

  it("never takes HTTPS when the shell declines a comparison", async () => {
    comparingShell([], {
      workspaceRequest: vi.fn(async () => ({ ok: false, reason: "unsupported_query" })),
    });
    for (const url of [CHANGES_URL, `${DIFF_URL}?comparison=cmp_1`]) {
      // oxlint-disable-next-line no-await-in-loop -- each endpoint in turn
      await expect(workspaceFetch(url, {}, { desktopOnly: true })).rejects.toMatchObject({
        reason: "unsupported_query",
      });
    }
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("names each comparison request, and cancels it when the page gives up", async () => {
    let finish: (value: unknown) => void = () => {};
    const workspaceRequest = vi.fn(
      (_request: { requestId?: string }) =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const { cancelWorkspaceRequest } = comparingShell(["cancel_requests"], { workspaceRequest });
    const controller = new AbortController();
    const pending = workspaceFetch(
      CHANGES_URL,
      { signal: controller.signal },
      { desktopOnly: true },
    );
    await vi.waitFor(() => expect(workspaceRequest).toHaveBeenCalled());
    const { requestId } = workspaceRequest.mock.lastCall![0] as { requestId?: string };
    expect(requestId).toMatch(/^[A-Za-z0-9_-]{1,64}$/);

    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(cancelWorkspaceRequest).toHaveBeenCalledWith({ requestId });
    // The late answer changes nothing.
    finish(await answered());

    workspaceRequest.mockImplementation(answered);
    await workspaceFetch(
      CHANGES_URL,
      { signal: new AbortController().signal },
      { desktopOnly: true },
    );
    const second = (workspaceRequest.mock.lastCall![0] as { requestId?: string }).requestId;
    expect(second).toBeDefined();
    expect(second).not.toBe(requestId);
  });

  it("sends no request ID, and no cancel, without both the capability and the method", async () => {
    for (const [capabilities, cancel] of [
      [["cancel_requests"], false],
      [[], true],
    ] as const) {
      let finish: (value: unknown) => void = () => {};
      const workspaceRequest = vi.fn(
        (_request: { requestId?: string }) =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      );
      const shell = comparingShell([...capabilities], { cancel, workspaceRequest });
      resetWorkspaceFastTrackForTests();
      const controller = new AbortController();
      const pending = workspaceFetch(
        CHANGES_URL,
        { signal: controller.signal },
        { desktopOnly: true },
      );
      // oxlint-disable-next-line no-await-in-loop -- each shell in turn
      await vi.waitFor(() => expect(workspaceRequest).toHaveBeenCalled());
      expect(workspaceRequest.mock.lastCall![0]).not.toHaveProperty("requestId");
      controller.abort();
      // oxlint-disable-next-line no-await-in-loop -- each shell in turn
      await expect(pending).rejects.toMatchObject({ name: "AbortError" });
      expect(shell.cancelWorkspaceRequest).not.toHaveBeenCalled();
      // oxlint-disable-next-line no-await-in-loop -- each step in turn
      finish(await answered());
    }
  });

  it("asks nothing when the page gave up before the request", async () => {
    const { workspaceRequest, cancelWorkspaceRequest } = comparingShell(["cancel_requests"]);
    const controller = new AbortController();
    controller.abort();
    await expect(
      workspaceFetch(CHANGES_URL, { signal: controller.signal }, { desktopOnly: true }),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(workspaceRequest).not.toHaveBeenCalled();
    expect(cancelWorkspaceRequest).not.toHaveBeenCalled();
  });
});

describe("streamWorkspaceSearch", () => {
  const SEARCH_URL = `/v1/sessions/${SESSION}/resources/environments/default/content-search?q=needle`;

  /** A shell that streams; `search` decides how each search goes. */
  function streamingShell(
    search: (request: unknown, onMatches: (matches: unknown[]) => void) => Promise<unknown>,
    { capabilities = ["content_search_stream", "cancel_requests"] } = {},
  ) {
    const shell = {
      workspaceRequest: vi.fn(answered),
      workspaceContentSearch: vi.fn(search),
      cancelWorkspaceRequest: vi.fn(async () => ({ ok: true })),
      getDesktopFeatures: async () => ({
        workspaceFastTrack: true,
        workspaceFastTrackCapabilities: capabilities,
      }),
    };
    installShell(shell);
    return shell;
  }

  it("streams a host-bound session's search, naming it and the files to search first", async () => {
    const shell = streamingShell(async (_request, onMatches) => {
      onMatches([{ path: "a.ts" }]);
      return { done: true, truncated: false, warning: null };
    });
    const heard: unknown[][] = [];
    const outcome = await streamWorkspaceSearch(SEARCH_URL, {
      focusedPaths: ["src/open.ts"],
      onMatches: (matches) => heard.push(matches),
    });
    expect(outcome).toEqual({ done: true, truncated: false, warning: null });
    expect(heard).toEqual([[{ path: "a.ts" }]]);
    const [request] = shell.workspaceContentSearch.mock.calls[0]!;
    expect(request).toEqual({
      url: SEARCH_URL,
      requestId: expect.any(String),
      focusedPaths: ["src/open.ts"],
      hideIgnored: true,
    });
  });

  it("declines without asking a shell that lacks the capability or the method", async () => {
    const without = streamingShell(async () => ({ done: true }), {
      capabilities: ["cancel_requests"],
    });
    expect(await streamWorkspaceSearch(SEARCH_URL, { onMatches: () => {} })).toEqual({
      declined: "unsupported",
    });
    expect(without.workspaceContentSearch).not.toHaveBeenCalled();
    resetWorkspaceFastTrackForTests();
    installShell({
      workspaceRequest: vi.fn(answered),
      getDesktopFeatures: async () => ({
        workspaceFastTrack: true,
        workspaceFastTrackCapabilities: ["content_search_stream"],
      }),
    });
    expect(await streamWorkspaceSearch(SEARCH_URL, { onMatches: () => {} })).toEqual({
      declined: "unsupported",
    });
  });

  it("declines a session that isn't host-bound, which no desktop helper can answer", async () => {
    const shell = streamingShell(async () => ({ done: true }));
    setSessionHost(SESSION, null);
    expect(await streamWorkspaceSearch(SEARCH_URL, { onMatches: () => {} })).toEqual({
      declined: "unsupported",
    });
    expect(shell.workspaceContentSearch).not.toHaveBeenCalled();
  });

  it("on abort tells the shell to stop, rejects at once, and drops late batches", async () => {
    let lateBatch: (() => void) | undefined;
    const shell = streamingShell(
      (_request, onMatches) =>
        new Promise(() => {
          lateBatch = () => onMatches([{ path: "late.ts" }]);
        }),
    );
    const controller = new AbortController();
    const heard: unknown[][] = [];
    const searching = streamWorkspaceSearch(SEARCH_URL, {
      signal: controller.signal,
      onMatches: (matches) => heard.push(matches),
    });
    await vi.waitFor(() => expect(shell.workspaceContentSearch).toHaveBeenCalled());
    controller.abort();
    await expect(searching).rejects.toMatchObject({ name: "AbortError" });
    const { requestId } = shell.workspaceContentSearch.mock.calls[0]![0] as { requestId: string };
    expect(shell.cancelWorkspaceRequest).toHaveBeenCalledWith({ requestId });
    lateBatch!();
    expect(heard).toEqual([]);
  });
});
