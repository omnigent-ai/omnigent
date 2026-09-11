// The GitHub info query refetches on the focused session's turn end
// (active → idle), so a PR the agent opened during the turn appears in the
// status-line indicator without opening the tab. This is the harness-agnostic
// replacement for the old per-tool git/gh signal — mirrors the trailing-edge
// invalidate proven in useWorkspaceChangedFiles.test.tsx.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, waitFor } from "@testing-library/react";
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
  fetchGithubFileContents,
  useGithubChangedFiles,
  useGithubInfo,
  useGithubPrDiff,
} from "./useGithub";

const onlineMock = vi.mocked(useSessionRunnerOnline);
const hostOnlineMock = vi.mocked(useSessionHostOnline);
const chatStoreMock = vi.mocked(useChatStore);
const fetchMock = vi.fn();

type StubStatus = "idle" | "running" | "waiting" | "failed";

function stubChatStore(conversationId: string | null, sessionStatus: StubStatus) {
  chatStoreMock.mockImplementation((selector: unknown) => {
    if (typeof selector === "function") {
      return (
        selector as (s: { conversationId: string | null; sessionStatus: StubStatus }) => unknown
      )({ conversationId, sessionStatus });
    }
    return undefined;
  });
}

function githubInfoResponse(): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => ({ object: "session.github.info", available: true }),
  } as unknown as Response;
}

function Probe({ id }: { id: string | undefined }) {
  useGithubInfo(id);
  return null;
}

function HostProbe({ workspace }: { workspace: string }) {
  useGithubInfo({ kind: "host", hostId: "host/a", workspace });
  return null;
}

function RevisionProbe({ revision }: { revision: string }) {
  const target = { kind: "host" as const, hostId: "host/a", workspace: "/repo" };
  useGithubChangedFiles(target, true, "https://github.com/acme/repo/pull/1", revision);
  useGithubPrDiff(target, true, "https://github.com/acme/repo/pull/1", revision);
  return null;
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(githubInfoResponse());
  vi.stubGlobal("fetch", fetchMock);
  onlineMock.mockReturnValue(true);
  hostOnlineMock.mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe("useGithubInfo turn-end invalidate", () => {
  it("preserves GitHub file diff parameters on the host route", async () => {
    await fetchGithubFileContents(
      { kind: "host", hostId: "host/a", workspace: "/repo one" },
      "src/a b.ts",
      "main",
      {
        pr_url: "https://github.com/acme/repo/pull/1",
        previous_path: "src/old.ts",
        head_sha: "head",
        base_sha: "base",
      },
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/hosts/host%2Fa/workspace/resources/github/diff/src/a%20b.ts?base=main&pr_url=https%3A%2F%2Fgithub.com%2Facme%2Frepo%2Fpull%2F1&previous_path=src%2Fold.ts&head_sha=head&base_sha=base&workspace=%2Frepo+one",
      expect.anything(),
    );
  });

  it("uses the host workspace route without a session liveness target", async () => {
    stubChatStore("conv_live", "running");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <HostProbe workspace="/repo one" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/hosts/host%2Fa/workspace/resources/github?workspace=%2Frepo+one",
    );
    expect(onlineMock).toHaveBeenCalledWith(undefined);
    expect(hostOnlineMock).toHaveBeenCalledWith(undefined);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/v1/sessions/"))).toBe(false);

    stubChatStore("conv_live", "idle");
    rerender(
      <QueryClientProvider client={qc}>
        <HostProbe workspace="/repo two" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/v1/hosts/host%2Fa/workspace/resources/github?workspace=%2Frepo+two",
    );
  });

  it("refetches PR files and patch when the remote revision changes", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <RevisionProbe revision="base:head-one" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    rerender(
      <QueryClientProvider client={qc}>
        <RevisionProbe revision="base:head-two" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
  });

  it("refetches github info when the focused session goes running → idle", async () => {
    stubChatStore("conv_live", "running");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Turn ends: the trailing invalidate fires one more github-info fetch.
    stubChatStore("conv_live", "idle");
    rerender(
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][0]).toBe("/v1/sessions/conv_live/resources/github");
  });

  it("does not refetch when status stays idle across renders", async () => {
    // Guard against a spurious refetch on every render — only the
    // active → idle transition triggers the trailing edge.
    stubChatStore("conv_live", "idle");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    rerender(
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
      </QueryClientProvider>,
    );
    await Promise.resolve();
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
