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
import {
  usePullRequestChangedFiles,
  usePullRequestDiff,
  usePullRequestInfo,
} from "./usePullRequests";

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

function Probe({
  id,
  poll = false,
  details = false,
}: {
  id: string;
  poll?: boolean;
  details?: boolean;
}) {
  usePullRequestInfo(id, { poll });
  usePullRequestChangedFiles(id, details);
  usePullRequestDiff(id, details);
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
  it.each([undefined, "not_synced"])(
    "reads saved changes and diff while offline (%s)",
    async (reason) => {
      vi.useFakeTimers();
      onlineMock.mockReturnValue(false);
      hostOnlineMock.mockReturnValue(false);
      const changes = {
        available: !reason,
        data: [],
        cached: true,
        cached_at: 1_700_000_000,
        ...(reason ? { unavailable_reason: reason } : {}),
      };
      const diff = {
        object: "session.github.pr_diff",
        patch: reason ? "" : "saved patch",
        cached: true,
        cached_at: 1_700_000_000,
        ...(reason ? { unavailable_reason: reason } : {}),
      };
      fetchMock.mockImplementation(
        async (input: string) =>
          new Response(
            JSON.stringify(
              input.endsWith("/changes")
                ? changes
                : input.endsWith("/diff")
                  ? diff
                  : {
                      object: "session.github.info",
                      available: false,
                      reason: "workspace_offline",
                      cached: true,
                    },
            ),
          ),
      );
      const client = new QueryClient();
      render(
        <QueryClientProvider client={client}>
          <Probe id="conv_saved" details poll />
        </QueryClientProvider>,
      );

      await vi.advanceTimersByTimeAsync(130_000);
      expect(client.getQueryData(["github-changed-files", "conv_saved"])).toEqual(changes);
      expect(client.getQueryData(["github-pr-diff", "conv_saved"])).toEqual(diff);
      expect(fetchMock).toHaveBeenCalledTimes(3);
    },
  );

  it("loads saved associations with both runner and host offline without polling", async () => {
    vi.useFakeTimers();
    onlineMock.mockReturnValue(false);
    hostOnlineMock.mockReturnValue(false);
    const url = "https://github.com/acme/app/pull/42";
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          object: "session.github.info",
          available: false,
          reason: "workspace_offline",
          tracking_available: true,
          selected_pr_url: url,
          prs: [
            {
              url,
              host: "github.com",
              repository: "acme/app",
              number: 42,
              relationship: "created",
            },
          ],
        }),
      ),
    );
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <Probe id="conv_asleep" poll />
      </QueryClientProvider>,
    );

    await vi.advanceTimersByTimeAsync(130_000);
    expect(client.getQueryData(["github-info", "conv_asleep"])).toMatchObject({
      selected_pr_url: url,
      prs: [{ number: 42 }],
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

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
