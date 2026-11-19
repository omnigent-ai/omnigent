// A runner-offline 503 from the GitHub resource retries only while something
// could still serve it; an asleep sandbox must not keep the panel spinning.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(),
  useSessionHostOnline: vi.fn(),
}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: vi.fn(),
}));

import { useSessionHostOnline, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useChatStore } from "@/store/chatStore";
import { usePullRequestInfo, usePullRequestInfos } from "./usePullRequests";

const onlineMock = vi.mocked(useSessionRunnerOnline);
const hostOnlineMock = vi.mocked(useSessionHostOnline);
const chatStoreMock = vi.mocked(useChatStore);
const fetchMock = vi.fn();

function runnerUnavailableResponse(): Response {
  return {
    ok: false,
    status: 503,
    statusText: "Service Unavailable",
    json: async () => ({ error: { code: "runner_unavailable" } }),
  } as unknown as Response;
}

function Probe({ id, batch = false }: { id: string; batch?: boolean }) {
  usePullRequestInfo(id);
  usePullRequestInfos(id, batch ? ["https://github.com/org/repo/pull/1"] : []);
  return null;
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(runnerUnavailableResponse());
  vi.stubGlobal("fetch", fetchMock);
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
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe("usePullRequestInfo runner-offline retry", () => {
  it.each([false, true])(
    "stops retrying once nothing can serve the session (batch=%s)",
    async (batch) => {
      vi.useFakeTimers();
      // Page load: liveness hasn't answered when the first 503 lands.
      onlineMock.mockReturnValue(undefined);
      hostOnlineMock.mockReturnValue(undefined);
      const qc = new QueryClient();
      const tree = () => (
        <QueryClientProvider client={qc}>
          <Probe id="conv_asleep" batch={batch} />
        </QueryClientProvider>
      );

      const view = render(tree());
      await vi.advanceTimersByTimeAsync(0);
      expect(fetchMock).toHaveBeenCalledTimes(batch ? 2 : 1);

      // An idle-stopped sandbox: runner and host both down.
      onlineMock.mockReturnValue(false);
      hostOnlineMock.mockReturnValue(false);
      view.rerender(tree());
      await vi.advanceTimersByTimeAsync(130_000);

      // The retry already scheduled at the first failure still runs; none after it.
      expect(fetchMock).toHaveBeenCalledTimes(batch ? 4 : 2);
    },
  );

  it("does not fetch linked PRs while the session is already asleep", async () => {
    vi.useFakeTimers();
    onlineMock.mockReturnValue(false);
    hostOnlineMock.mockReturnValue(false);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Probe id="conv_asleep" batch />
      </QueryClientProvider>,
    );
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps retrying while the host is up and the runner is still connecting", async () => {
    vi.useFakeTimers();
    onlineMock.mockReturnValue(false);
    hostOnlineMock.mockReturnValue(true);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Probe id="conv_booting" />
      </QueryClientProvider>,
    );

    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(3_500);

    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3);
  });
});
