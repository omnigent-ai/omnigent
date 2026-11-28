// A host that doesn't answer the GitHub resource is retried as one bounded
// episode per read, shared by its observers and kept across fetches.

import { focusManager, hashKey, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(),
  useSessionHostOnline: vi.fn(),
}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: vi.fn(),
}));

import { useSessionHostOnline, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import {
  clearSessionHostTransportRecovery,
  exhaustedHostTransportError,
  HOST_TRANSPORT_RETRIES,
  hostTransportFailures,
  isHostTransportError,
  recordHostTransportFailure,
  resetHostTransportRecovery,
} from "@/lib/hostTransportRecovery";
import { useChatStore } from "@/store/chatStore";
import { resetHostReturns, useHostReturnObserver } from "@/store/hostReturns";
import { resetSandboxResumes, useSandboxResumeStore } from "@/store/sandboxResume";
import { type PullRequestInfo, usePullRequestInfo } from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const runnerOnlineMock = vi.mocked(useSessionRunnerOnline);
const hostOnlineMock = vi.mocked(useSessionHostOnline);
const chatStoreMock = vi.mocked(useChatStore);
const fetchMock = vi.fn();

const SESSION = "conv_resumed";
const PR_URL = "https://github.com/acme/app/pull/7";
const RETRY_DELAYS = [1_000, 2_000, 4_000, 8_000, 15_000, 15_000];

const READY: PullRequestInfo = {
  object: "session.github.info",
  available: true,
  provider: "github",
  branch: "feat/x",
  repo: { name_with_owner: "acme/app" },
  authenticated: true,
  gh_available: true,
  pr: null,
};

function gateway(status = 502): Response {
  return {
    ok: false,
    status,
    statusText: "Bad Gateway",
    json: async () => {
      throw new SyntaxError("not JSON");
    },
  } as unknown as Response;
}

function answer(info: PullRequestInfo): Response {
  return { ok: true, status: 200, json: async () => info } as unknown as Response;
}

/** Calls that reached the session's host. */
function hostCalls(): number {
  return fetchMock.mock.calls.filter(([input]) => String(input).includes("/resources/github"))
    .length;
}

function Probe({ prUrl }: { name?: string; prUrl?: string }) {
  usePullRequestInfo(SESSION, prUrl ? { prUrl } : undefined);
  return null;
}

const infoKey = (prUrl?: string) => ["pr-page-github-info", SESSION, ...(prUrl ? [prUrl] : [])];
const readState = (prUrl?: string) => client.getQueryState<PullRequestInfo>(infoKey(prUrl));
/** Ask for the session's info again the way the app does, without waiting for it. */
const invalidate = () =>
  void client.invalidateQueries({ queryKey: ["pr-page-github-info", SESSION] });

function Observer({ hostOnline }: { hostOnline: Map<string, boolean | null> }) {
  useHostReturnObserver(hostOnline);
  return null;
}

function setServeable(serveable: boolean) {
  runnerOnlineMock.mockReturnValue(serveable);
  hostOnlineMock.mockReturnValue(serveable);
}

/** Advance through every retry of an episode that keeps failing. */
async function exhaust() {
  await vi.advanceTimersByTimeAsync(0);
  // Timers each retry schedules run within the same advance.
  await vi.advanceTimersByTimeAsync(RETRY_DELAYS.reduce((total, delay) => total + delay, 0));
}

let client: QueryClient;
const withClient = (ui: React.ReactNode) => (
  <QueryClientProvider client={client}>{ui}</QueryClientProvider>
);

beforeEach(() => {
  vi.useFakeTimers();
  client = new QueryClient();
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(gateway());
  vi.stubGlobal("fetch", fetchMock);
  setServeable(true);
  chatStoreMock.mockImplementation((selector: unknown) =>
    typeof selector === "function"
      ? (selector as (s: { conversationId: null; sessionStatus: "idle" }) => unknown)({
          conversationId: null,
          sessionStatus: "idle",
        })
      : undefined,
  );
});

afterEach(() => {
  cleanup();
  focusManager.setFocused(undefined);
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.resetAllMocks();
  resetHostTransportRecovery();
  resetHostReturns();
  resetSandboxResumes();
});

describe("usePullRequestInfo host recovery", () => {
  it("retries a gateway failure six times, after 1, 2, 4, 8, 15 and 15 seconds", async () => {
    render(withClient(<Probe />));
    await vi.advanceTimersByTimeAsync(0);
    expect(hostCalls()).toBe(1);
    for (const [retry, delay] of RETRY_DELAYS.entries()) {
      // Each retry is checked at its own moment, so the waits run in order.
      // oxlint-disable-next-line no-await-in-loop
      await vi.advanceTimersByTimeAsync(delay - 1);
      expect(hostCalls(), `before retry ${retry + 1}`).toBe(retry + 1);
      // oxlint-disable-next-line no-await-in-loop
      await vi.advanceTimersByTimeAsync(1);
      expect(hostCalls(), `retry ${retry + 1}`).toBe(retry + 2);
    }
    await vi.advanceTimersByTimeAsync(60_000);
    expect(hostCalls()).toBe(7);
    expect(isHostTransportError(readState()?.error)).toBe(true);
    expect(readState()?.data).toBeUndefined();
  });

  it("recovers within the episode once the host answers", async () => {
    fetchMock
      .mockResolvedValueOnce(gateway(504))
      .mockResolvedValueOnce(gateway(503))
      .mockResolvedValue(answer(READY));
    render(withClient(<Probe />));
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1_000);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(hostCalls()).toBe(3);
    expect(readState()?.data?.repo?.name_with_owner).toBe("acme/app");
    expect(hostTransportFailures(hashKey(["pr-page-github-info", SESSION]))).toBe(0);
  });

  it.each(["no cached info", "cached info"])(
    "reads nothing automatically once the episode is used up, with %s",
    async (cache) => {
      if (cache === "cached info") fetchMock.mockResolvedValueOnce(answer(READY));
      const view = render(withClient(<Probe />));
      await vi.advanceTimersByTimeAsync(0);
      if (cache === "cached info") {
        expect(readState()?.data).toBeDefined();
        invalidate();
      }
      await exhaust();
      const used = hostCalls();
      expect(used).toBe(cache === "cached info" ? 8 : 7);
      // Cached info keeps showing beside the error.
      expect(isHostTransportError(readState()?.error)).toBe(true);
      expect(readState()?.data === undefined).toBe(cache === "no cached info");

      // Window blur and focus, a remount, the rail reopening and the turn-end invalidation.
      act(() => focusManager.setFocused(false));
      act(() => focusManager.setFocused(true));
      view.unmount();
      render(withClient(<Probe />));
      void client.refetchQueries({ queryKey: ["pr-page-github-info", SESSION], stale: true });
      invalidate();
      await vi.advanceTimersByTimeAsync(120_000);
      expect(hostCalls()).toBe(used);

      // Check again starts a fresh episode.
      fetchMock.mockResolvedValue(answer(READY));
      clearSessionHostTransportRecovery(SESSION);
      invalidate();
      await vi.advanceTimersByTimeAsync(0);
      expect(hostCalls()).toBe(used + 1);
      expect(readState()?.error).toBeNull();
    },
  );

  it("keeps a separate episode for each selected pull request", async () => {
    render(withClient(<Probe />));
    await exhaust();
    expect(hostCalls()).toBe(7);
    fetchMock.mockResolvedValue(answer({ ...READY, selected_pr_url: PR_URL }));
    render(withClient(<Probe name="selected" prUrl={PR_URL} />));
    await vi.advanceTimersByTimeAsync(0);
    expect(hostCalls()).toBe(8);
    expect(readState(PR_URL)?.data?.selected_pr_url).toBe(PR_URL);
  });

  it("doesn't count a cancelled read against the episode", async () => {
    fetchMock.mockImplementation(
      (_input: unknown, init?: RequestInit) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
    render(withClient(<Probe />));
    await vi.advanceTimersByTimeAsync(0);
    expect(hostCalls()).toBe(1);
    await client.cancelQueries({ queryKey: ["pr-page-github-info", SESSION] });
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(hostTransportFailures(hashKey(["pr-page-github-info", SESSION]))).toBe(0);
  });

  it("makes no host call once the gate closes during a retry's wait, after an observer left", async () => {
    const view = render(
      withClient(
        <>
          <Probe name="rail" />
          <Probe name="panel" />
        </>,
      ),
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(hostCalls()).toBe(1);
    // The rail closes; the panel keeps the read and its retries.
    view.rerender(withClient(<Probe name="panel" />));
    await vi.advanceTimersByTimeAsync(1_000);
    expect(hostCalls()).toBe(2);
    // The host goes down while the next retry waits.
    setServeable(false);
    view.rerender(withClient(<Probe name="panel" />));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(hostCalls()).toBe(2);
  });

  it("starts a fresh episode and reads once when the host comes back", async () => {
    const offline = new Map([[SESSION, false]]);
    const online = new Map([[SESSION, true]]);
    const view = render(
      withClient(
        <>
          <Observer hostOnline={new Map([[SESSION, true]])} />
          <Probe />
        </>,
      ),
    );
    await exhaust();
    expect(hostCalls()).toBe(7);
    fetchMock.mockResolvedValue(answer(READY));
    view.rerender(
      withClient(
        <>
          <Observer hostOnline={offline} />
          <Probe />
        </>,
      ),
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(hostCalls()).toBe(7);
    // The resume and the liveness both report the same return: one read.
    act(() => useSandboxResumeStore.setState({ resumedAt: { [SESSION]: Date.now() } }));
    view.rerender(
      withClient(
        <>
          <Observer hostOnline={online} />
          <Probe />
        </>,
      ),
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(hostCalls()).toBe(8);
    expect(readState()?.data?.repo?.name_with_owner).toBe("acme/app");
  });

  it("doesn't read again when a host first reads online", async () => {
    fetchMock.mockResolvedValue(answer(READY));
    const view = render(
      withClient(
        <>
          <Observer hostOnline={new Map()} />
          <Probe />
        </>,
      ),
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(hostCalls()).toBe(1);
    view.rerender(
      withClient(
        <>
          <Observer hostOnline={new Map([[SESSION, true]])} />
          <Probe />
        </>,
      ),
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(hostCalls()).toBe(1);
  });

  it("doesn't count a read cancelled while its error body arrives, and recovers after it", async () => {
    const key = hashKey(infoKey());
    for (let failure = 0; failure < HOST_TRANSPORT_RETRIES; failure++) {
      recordHostTransportFailure(key, SESSION, gateway());
    }
    fetchMock.mockImplementationOnce(
      async (_input: unknown, init?: RequestInit) =>
        ({
          ok: false,
          status: 502,
          statusText: "Bad Gateway",
          json: () =>
            new Promise((_resolve, reject) => {
              init?.signal?.addEventListener("abort", () =>
                reject(new DOMException("aborted", "AbortError")),
              );
            }),
        }) as unknown as Response,
    );
    render(withClient(<Probe />));
    await vi.advanceTimersByTimeAsync(0);
    expect(hostCalls()).toBe(1);
    await client.cancelQueries({ queryKey: infoKey() });
    await vi.advanceTimersByTimeAsync(0);
    expect(hostTransportFailures(key)).toBe(HOST_TRANSPORT_RETRIES);
    expect(exhaustedHostTransportError(key)).toBeUndefined();

    fetchMock.mockResolvedValue(answer(READY));
    invalidate();
    await vi.advanceTimersByTimeAsync(0);
    expect(hostCalls()).toBe(2);
    expect(hostTransportFailures(key)).toBe(0);
  });
});
