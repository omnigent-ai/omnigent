import { Code, ConnectError } from "@connectrpc/connect";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  callPullRequestMethod: vi.fn(),
  callPullRequestWrite: vi.fn(),
  servingRoutes: vi.fn(),
}));
vi.mock("@/lib/workspaceApi", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceApiModule>()),
  ...api,
}));

import { HostUnreachableError } from "@/lib/hostTransportRecovery";
import type * as WorkspaceApiModule from "@/lib/workspaceApi";
import { RunnerOfflineError } from "@/lib/workspaceReadErrors";
import {
  PullRequestApiError,
  PullRequestMethodUnservedError,
  readPullRequestRpc,
  readPullRequestViaWorkspaceApi,
  writePullRequestRpc,
} from "./pullRequestRpc";

const SESSION = "conv_1";
const read = vi.fn(async () => ({ payload: { ok: true } }));
const route = (served: string[]) => ({
  route: "server",
  key: "server:conv_1",
  served: new Set(served),
});

beforeEach(() => {
  vi.resetAllMocks();
  api.servingRoutes.mockResolvedValue({ serving: [], failures: [] });
});

describe("reading through the workspace API", () => {
  it("answers with the method's payload", async () => {
    api.callPullRequestMethod.mockResolvedValue({ ok: true });
    await expect(readPullRequestRpc(SESSION, "getPullRequest", read)).resolves.toEqual({
      ok: true,
    });
  });

  it("gives a refusal the status REST would have answered with", async () => {
    api.callPullRequestMethod.mockRejectedValue(new ConnectError("no such PR", Code.NotFound));
    const error = await readPullRequestRpc(SESSION, "getPullRequest", read).catch((e) => e);
    expect(error).toBeInstanceOf(PullRequestApiError);
    expect(error).toMatchObject({ status: 404, message: "no such PR" });
    expect(error).not.toBeInstanceOf(PullRequestMethodUnservedError);
  });

  it("reads an unknown refusal as a server error", async () => {
    api.callPullRequestMethod.mockRejectedValue(new ConnectError("boom", Code.Internal));
    await expect(readPullRequestRpc(SESSION, "getPullRequest", read)).rejects.toMatchObject({
      status: 500,
    });
  });

  it("passes other failures through unchanged", async () => {
    const failure = new Error("not a connect error");
    api.callPullRequestMethod.mockRejectedValue(failure);
    await expect(readPullRequestRpc(SESSION, "getPullRequest", read)).rejects.toBe(failure);
  });

  it("leaves an unserved read to the caller where it has another path", async () => {
    api.callPullRequestMethod.mockResolvedValue(null);
    await expect(
      readPullRequestViaWorkspaceApi(SESSION, "getPullRequest", read),
    ).resolves.toBeNull();
    expect(api.servingRoutes).not.toHaveBeenCalled();
  });
});

describe("a call no route served", () => {
  beforeEach(() => {
    api.callPullRequestMethod.mockResolvedValue(null);
    api.callPullRequestWrite.mockResolvedValue(null);
  });

  it("asks for a host update when the host answers without the method", async () => {
    api.servingRoutes.mockResolvedValue({ serving: [route(["ListLabels"])], failures: [] });
    const error = await readPullRequestRpc(SESSION, "getPullRequest", read).catch((e) => e);
    expect(error).toBeInstanceOf(PullRequestMethodUnservedError);
    expect(error).toMatchObject({ method: "getPullRequest", status: 501 });
    expect((error as Error).message).toMatch(/Update the host/);
  });

  it("reads a host that lists the method but didn't answer as not reached", async () => {
    api.servingRoutes.mockResolvedValue({ serving: [route(["GetPullRequest"])], failures: [] });
    await expect(readPullRequestRpc(SESSION, "getPullRequest", read)).rejects.toBeInstanceOf(
      HostUnreachableError,
    );
  });

  it("reads an unavailable host as its runner being offline", async () => {
    api.servingRoutes.mockResolvedValue({
      serving: [],
      failures: [new ConnectError("runner unavailable", Code.Unavailable)],
    });
    await expect(readPullRequestRpc(SESSION, "getPullRequest", read)).rejects.toBeInstanceOf(
      RunnerOfflineError,
    );
  });

  it("reads a network failure as the host not being reached", async () => {
    api.servingRoutes.mockResolvedValue({ serving: [], failures: [new TypeError("fetch failed")] });
    await expect(readPullRequestRpc(SESSION, "getPullRequest", read)).rejects.toBeInstanceOf(
      HostUnreachableError,
    );
  });

  it("explains an unsent write the same way, and sends nothing else", async () => {
    api.servingRoutes.mockResolvedValue({ serving: [route([])], failures: [] });
    await expect(
      writePullRequestRpc(SESSION, { method: "createComment", call: read }),
    ).rejects.toBeInstanceOf(PullRequestMethodUnservedError);
    expect(api.callPullRequestWrite).toHaveBeenCalledTimes(1);
  });
});
