import { beforeEach, expect, it, vi } from "vitest";
import {
  isLoopbackPreviewUrl,
  previewHostOptions,
  withPreviewHostRetry,
} from "./previewHostOptions";
import { fetchHosts } from "@/hooks/useHosts";
import { getDesktopFeatures } from "./nativeBridge";
import type * as NativeBridge from "./nativeBridge";
import { getSessionSlim } from "./sessionsApi";
import { fetchSessionHostId } from "./fetchSessionHostId";

vi.mock("@/hooks/useHosts", () => ({ fetchHosts: vi.fn() }));
vi.mock("./sessionsApi", () => ({ getSessionSlim: vi.fn() }));
vi.mock("./nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  getDesktopFeatures: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getSessionSlim).mockReset();
  vi.mocked(getDesktopFeatures).mockResolvedValue({ sshLocalhostPreview: true });
  vi.mocked(fetchHosts).mockResolvedValue([
    { host_id: "remote", name: "devbox", status: "online" } as Awaited<
      ReturnType<typeof fetchHosts>
    >[number],
  ]);
});

it("adds an SSH route hint to a remote localhost navigation in a capable shell", async () => {
  expect(await previewHostOptions("remote", "http://dev.localhost:8787")).toEqual({
    sourceHostId: "remote",
    sessionHostProtocol: 1,
    sshPreviewProtocol: 1,
    sourceSshTarget: "devbox",
  });
});

it.each([null, {}, { sshLocalhostPreview: false }])(
  "keeps an older or absent shell on its existing path: %j",
  async (features) => {
    vi.mocked(getDesktopFeatures).mockResolvedValue(features);
    expect(await previewHostOptions("remote", "http://localhost:8787")).toEqual({
      sourceHostId: "remote",
      sessionHostProtocol: 1,
    });
    expect(fetchHosts).not.toHaveBeenCalled();
  },
);

it.each(["https://example.com", "http://127.0.0.2:8787", "file:///tmp/test", "bad URL"])(
  "advertises preview support without discovering SSH for %s",
  async (url) => {
    expect(await previewHostOptions("remote", url)).toEqual({
      sourceHostId: "remote",
      sessionHostProtocol: 1,
      sshPreviewProtocol: 1,
    });
    expect(fetchHosts).not.toHaveBeenCalled();
  },
);

it("keeps unknown provenance and discovery failures public-only", async () => {
  expect(await previewHostOptions(null, "http://localhost:8787")).toEqual({});
  vi.mocked(fetchHosts).mockRejectedValue(new Error("offline"));
  expect(await previewHostOptions("remote", "http://127.0.0.1:8787")).toEqual({
    sourceHostId: "remote",
    sessionHostProtocol: 1,
    sshPreviewProtocol: 1,
  });
});

it.each([
  { host_id: "remote", name: "devbox", status: "offline" },
  { host_id: "another", name: "devbox", status: "online" },
  { host_id: "remote", name: "not an ssh destination", status: "online" },
  { host_id: "remote", name: "devbox", status: "online", sandbox_provider: "container" },
])("does not suggest a destination for %j", async (host) => {
  vi.mocked(fetchHosts).mockResolvedValue([host as Awaited<ReturnType<typeof fetchHosts>>[number]]);
  expect(await previewHostOptions("remote", "http://localhost:8787")).toEqual({
    sourceHostId: "remote",
    sessionHostProtocol: 1,
    sshPreviewProtocol: 1,
  });
});

it("aborts stalled discovery and keeps navigation on the existing policy path", async () => {
  let signal: AbortSignal | undefined;
  vi.mocked(fetchHosts).mockImplementation(
    async (_includeSandbox, _sourceHostId, requestSignal) => {
      signal = requestSignal;
      return new Promise((_resolve, reject) => {
        signal!.addEventListener("abort", () => reject(signal!.reason), { once: true });
      });
    },
  );
  expect(await previewHostOptions("remote", "http://localhost:8787")).toEqual({
    sourceHostId: "remote",
    sessionHostProtocol: 1,
    sshPreviewProtocol: 1,
  });
  expect(signal?.aborted).toBe(true);
});

it("refreshes provenance after consent and retries only once for the same host", async () => {
  const steps: string[] = [];
  vi.mocked(getSessionSlim).mockImplementation(async (id) => {
    steps.push("refresh");
    return { id, hostId: "host-a", parentSessionId: null } as Awaited<
      ReturnType<typeof getSessionSlim>
    >;
  });
  const navigate = vi.fn(async () => {
    steps.push("navigate");
    return { ok: false, retryWithFreshHost: true, navigationRetryToken: "continuation" };
  });
  await withPreviewHostRetry("session", "host-a", navigate);
  expect(steps).toEqual(["navigate", "refresh", "navigate"]);
  expect(navigate).toHaveBeenCalledTimes(2);
  expect(navigate).toHaveBeenNthCalledWith(1);
  expect(navigate).toHaveBeenNthCalledWith(2, { navigationRetryToken: "continuation" });
});

it.each([false, true])(
  "refuses a moved host after deferred consent (inherited: %s)",
  async (inherited) => {
    let currentHost = "host-a";
    vi.mocked(getSessionSlim).mockImplementation(
      async (id) =>
        ({
          id,
          hostId: inherited && id === "session" ? null : currentHost,
          parentSessionId: inherited && id === "session" ? "parent" : null,
        }) as Awaited<ReturnType<typeof getSessionSlim>>,
    );
    let resolveConsent!: (result: NativeBridge.BrowserNavigationResult) => void;
    const consent = new Promise<NativeBridge.BrowserNavigationResult>((resolve) => {
      resolveConsent = resolve;
    });
    const navigate = vi.fn(() => consent);
    const pending = withPreviewHostRetry("session", "host-a", navigate);
    expect(getSessionSlim).not.toHaveBeenCalled();
    currentHost = "host-b";
    resolveConsent({ ok: false, retryWithFreshHost: true, navigationRetryToken: "continuation" });
    expect(await pending).toMatchObject({
      ok: false,
      retryWithFreshHost: false,
      error: expect.stringContaining("host changed"),
    });
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(vi.mocked(getSessionSlim).mock.calls.map(([id]) => id)).toEqual(
      inherited ? ["session", "parent"] : ["session"],
    );
  },
);

it("does not retry if current provenance is unavailable", async () => {
  vi.mocked(getSessionSlim).mockRejectedValue(new Error("offline"));
  const navigate = vi.fn().mockResolvedValue({
    ok: false,
    retryWithFreshHost: true,
    navigationRetryToken: "continuation",
  });
  expect(await withPreviewHostRetry("session", "host-a", navigate)).toMatchObject({
    ok: false,
    retryWithFreshHost: false,
  });
  expect(navigate).toHaveBeenCalledTimes(1);
});

it("does not reuse a host lookup that started before consent", async () => {
  type Session = Awaited<ReturnType<typeof getSessionSlim>>;
  let resolveEarlier!: (session: Session) => void;
  const oldResponse = new Promise<Session>((resolve) => {
    resolveEarlier = resolve;
  });
  vi.mocked(getSessionSlim)
    .mockReturnValueOnce(oldResponse)
    .mockResolvedValue({ id: "session", hostId: "host-b", parentSessionId: null } as Session);
  const earlier = fetchSessionHostId("session");
  const consent = Promise.resolve({
    ok: false,
    retryWithFreshHost: true,
    navigationRetryToken: "continuation",
  });
  const navigate = vi.fn(() => consent);
  const pending = withPreviewHostRetry("session", "host-a", navigate);
  await consent;
  resolveEarlier({ id: "session", hostId: "host-a", parentSessionId: null } as Session);
  await earlier;
  expect(await pending).toMatchObject({ ok: false, retryWithFreshHost: false });
  expect(navigate).toHaveBeenCalledTimes(1);
  expect(getSessionSlim).toHaveBeenCalledTimes(2);
});

it.each([undefined, ""])(
  "does not resume without a native continuation token: %j",
  async (token) => {
    const navigate = vi.fn().mockResolvedValue({
      ok: false,
      retryWithFreshHost: true,
      navigationRetryToken: token,
    });
    expect(await withPreviewHostRetry("session", "host-a", navigate)).toMatchObject({
      ok: false,
      retryWithFreshHost: false,
    });
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(getSessionSlim).not.toHaveBeenCalled();
  },
);

it.each([{ ok: true }, { ok: false, error: "blocked" }, null, undefined])(
  "does not refresh or retry unrelated results: %j",
  async (result) => {
    const navigate = vi.fn().mockResolvedValue(result);
    expect(await withPreviewHostRetry("session", "host-a", navigate)).toEqual(result);
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(getSessionSlim).not.toHaveBeenCalled();
  },
);

it.each([
  ["http://localhost:8787", true],
  ["https://dev.localhost:8787", true],
  ["http://127.0.0.1", true],
  ["http://[::1]:8787", true],
  ["https://localhost.example", false],
  ["http://127.0.0.2", false],
  ["file://localhost/tmp/page", false],
  ["bad URL", false],
])("recognizes preview URL %s: %s", (url, expected) => {
  expect(isLoopbackPreviewUrl(url)).toBe(expected);
});

it.each([false, true])(
  "negotiates settings-based previews independently of SSH: %s",
  async (ssh) => {
    vi.mocked(getDesktopFeatures).mockResolvedValue({
      trustedRemoteLocalhostPreview: true,
      sshLocalhostPreview: ssh,
    });
    expect(await previewHostOptions("remote", "https://example.com")).toEqual({
      sourceHostId: "remote",
      sessionHostProtocol: 1,
      trustedRemotePreviewProtocol: 1,
      ...(ssh ? { sshPreviewProtocol: 1 } : {}),
    });
    expect(fetchHosts).not.toHaveBeenCalled();
  },
);
