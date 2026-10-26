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
import { usePullRequestInfo } from "./usePullRequests";

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

function Probe({ id }: { id: string }) {
  usePullRequestInfo(id);
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
  it("stops retrying once the health poll shows nothing can serve the session", async () => {
    vi.useFakeTimers();
    // Page load: liveness hasn't answered when the first 503 lands.
    onlineMock.mockReturnValue(undefined);
    hostOnlineMock.mockReturnValue(undefined);
    const qc = new QueryClient();
    const tree = () => (
      <QueryClientProvider client={qc}>
        <Probe id="conv_asleep" />
      </QueryClientProvider>
    );

    const view = render(tree());
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // An idle-stopped sandbox: runner and host both down.
    onlineMock.mockReturnValue(false);
    hostOnlineMock.mockReturnValue(false);
    view.rerender(tree());
    await vi.advanceTimersByTimeAsync(130_000);

    // The retry already scheduled at the first failure still runs; none after it.
    expect(fetchMock).toHaveBeenCalledTimes(2);
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
