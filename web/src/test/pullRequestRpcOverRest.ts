// Answers the PR page's workspace API calls through the session's `/resources/github` REST
// route, so a test that stubs `fetch` with REST answers drives the RPC path unchanged:
//
//   vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
//     (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
//   );
//
// Each method maps to the REST request it stands for, with the same path, query and body.

import { HostUnreachableError } from "@/lib/hostTransportRecovery";
import { authenticatedFetch } from "@/lib/identity";
import type * as PullRequestRpcModule from "@/lib/pullRequestRpc";
import type { Method, WorkspaceClient } from "@/lib/workspaceApi";
import { isRunnerUnavailable503, RunnerOfflineError } from "@/lib/workspaceReadErrors";

type Request = Record<string, unknown>;

interface RestCall {
  resource: string;
  query?: Record<string, unknown>;
  body?: Record<string, unknown>;
  form?: FormData;
}

const text = (value: unknown) => (typeof value === "string" ? value : "");

/** A query with REST's names, leaving out empty values. */
function query(fields: [string, unknown][]): Record<string, unknown> {
  return Object.fromEntries(fields.filter(([, value]) => value !== "" && value != null));
}

const REST_FOR: Partial<Record<Method, (request: Request) => RestCall>> = {
  getPullRequest: ({ prUrl }) => ({ resource: "", query: query([["pr_url", prUrl]]) }),
  listPullRequestFiles: ({ prUrl, baseSha, headSha }) => ({
    resource: "/changes",
    query: query([
      ["pr_url", prUrl],
      ["base_sha", baseSha],
      ["head_sha", headSha],
    ]),
  }),
  getPullRequestDiff: (request) =>
    text(request.path)
      ? {
          resource: `/diff/${text(request.path).split("/").map(encodeURIComponent).join("/")}`,
          query: query([
            ["base", request.base],
            ["pr_url", request.prUrl],
            ["previous_path", request.previousPath],
            ["head_sha", request.headSha],
            ["base_sha", request.baseSha],
          ]),
        }
      : {
          resource: "/diff",
          query: query([
            ["pr_url", request.prUrl],
            ["base_sha", request.baseSha],
            ["head_sha", request.headSha],
          ]),
        },
  getCommitDiff: ({ prUrl, commitSha }) => ({
    resource: "/commit-diff",
    query: query([
      ["pr_url", prUrl],
      ["commit_sha", commitSha],
    ]),
  }),
  comparePullRequestRange: ({ prUrl, headSha, fromSha }) => ({
    resource: "/compare-diff",
    query: query([
      ["pr_url", prUrl],
      ["head_sha", headSha],
      ["from_sha", fromSha],
    ]),
  }),
  listPullRequestCommits: ({ prUrl, headSha }) => ({
    resource: "/commits",
    query: query([
      ["pr_url", prUrl],
      ["head_sha", headSha],
    ]),
  }),
  listReviewThreads: ({ prUrl, pageToken, headSha, baseSha }) => ({
    resource: "/review-threads",
    query: query([
      ["pr_url", prUrl],
      ["after", pageToken],
      ["head_sha", pageToken ? headSha : ""],
      ["base_sha", pageToken ? baseSha : ""],
    ]),
  }),
  listReviewThreadComments: ({ prUrl, threadId, pageToken }) => ({
    resource: `/review-threads/${encodeURIComponent(text(threadId))}/comments`,
    query: query([
      ["pr_url", prUrl],
      ["after", pageToken],
    ]),
  }),
  listLabels: ({ prUrl }) => ({ resource: "/labels", query: query([["pr_url", prUrl]]) }),
  getMergeOptions: ({ prUrl }) => ({
    resource: "/merge-options",
    query: query([["pr_url", prUrl]]),
  }),
  listReviewerCandidates: ({ prUrl }) => ({
    resource: "/reviewers",
    query: query([["pr_url", prUrl]]),
  }),
  updateLabels: ({ prUrl, add, remove }) => ({
    resource: "/labels",
    body: { pr_url: prUrl, add, remove },
  }),
  updateReviewers: ({ prUrl, add, remove }) => ({
    resource: "/reviewers",
    body: { pr_url: prUrl, add, remove },
  }),
  updatePullRequestDetails: ({ prUrl, title, body }) => ({
    resource: "/details",
    body: {
      pr_url: prUrl,
      ...(title === undefined ? {} : { title }),
      ...(body === undefined ? {} : { body }),
    },
  }),
  mergePullRequest: ({ prUrl, mergeMethod, expectedHeadOid }) => ({
    resource: "/merge",
    body: { pr_url: prUrl, merge_method: mergeMethod, expected_head: expectedHeadOid },
  }),
  mergePullRequestAsAccount: ({ prUrl, mergeMethod, expectedHeadOid, account }) => ({
    resource: "/merge-as",
    body: { pr_url: prUrl, merge_method: mergeMethod, expected_head: expectedHeadOid, account },
  }),
  disableAutoMerge: ({ prUrl }) => ({ resource: "/disable-auto-merge", body: { pr_url: prUrl } }),
  createComment: ({ prUrl, body }) => ({ resource: "/comments", body: { pr_url: prUrl, body } }),
  replyToReviewThread: ({ prUrl, threadId, body }) => ({
    resource: `/review-threads/${encodeURIComponent(text(threadId))}/replies`,
    body: { pr_url: prUrl, body },
  }),
  createReviewThread: ({ prUrl, body, anchor }) => {
    const { path, side, line, startLine, headSha, baseSha } = anchor as Request;
    return {
      resource: "/review-threads",
      body: {
        pr_url: prUrl,
        body,
        anchor: {
          path,
          side,
          line,
          start_line: startLine ?? null,
          head_sha: headSha,
          base_sha: baseSha,
        },
      },
    };
  },
  setPullRequestAccount: ({ prUrl, account, remote }) => ({
    resource: "/preferences",
    body: query([
      ["pr_url", prUrl],
      ["account", account],
      ["remote", remote],
    ]),
  }),
  uploadAttachment: ({ prUrl, name, content }) => {
    const form = new FormData();
    form.append("pr_url", text(prUrl));
    form.append("file", new Blob([content as Uint8Array<ArrayBuffer>]), text(name));
    return { resource: "/attachments", form };
  },
};

/** The REST request; no answer at all reads as the host not being reached, as on the RPC path. */
async function sendRest(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await authenticatedFetch(path, init);
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
    throw new HostUnreachableError(error);
  }
}

/** The REST answer to `call` on `conversationId`'s session, as the method's payload. */
async function overRest(
  module: typeof PullRequestRpcModule,
  conversationId: string,
  method: Method,
  call: PullRequestRpcModule.PullRequestRpc["call"],
  signal?: AbortSignal,
): Promise<unknown> {
  let rest: RestCall | null = null;
  let callSignal: AbortSignal | undefined;
  const client = new Proxy(
    {},
    {
      get: (_target, name) => (request: Request, options?: { signal?: AbortSignal }) => {
        callSignal = options?.signal;
        const toRest = REST_FOR[name as Method];
        if (!toRest) throw new Error(`No REST route stands for ${String(name)}`);
        rest = toRest(request);
        return Promise.resolve({ payload: {} });
      },
    },
  ) as WorkspaceClient;
  await call(client);
  if (!rest) throw new Error(`${method} made no call`);
  const { resource, query: params, body, form } = rest as RestCall;
  const search =
    params && Object.keys(params).length
      ? `?${new URLSearchParams(params as Record<string, string>)}`
      : "";
  const path = `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github${resource}${search}`;
  const response = await sendRest(
    path,
    form
      ? { method: "POST", body: form }
      : body
        ? {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }
        : { signal: signal ?? callSignal },
  );
  if (response.status === 503 && (await isRunnerUnavailable503(response))) {
    throw new RunnerOfflineError();
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const answer = (await response.json()) as { error?: { message?: string }; detail?: unknown };
      if (answer?.error?.message) message = answer.error.message;
      else if (typeof answer?.detail === "string" && answer.detail) message = answer.detail;
    } catch {
      // Not JSON: keep the status line.
    }
    throw new module.PullRequestApiError(message, response.status);
  }
  return response.json();
}

/** `@/lib/pullRequestRpc` with its calls answered through REST. */
export function pullRequestRpcOverRest(module: typeof PullRequestRpcModule) {
  const read = (<T>(
    conversationId: string,
    method: Method,
    call: PullRequestRpcModule.PullRequestRpc["call"],
    signal?: AbortSignal,
  ) =>
    overRest(
      module,
      conversationId,
      method,
      call,
      signal,
    ) as Promise<T>) as typeof module.readPullRequestRpc;
  const write = (<T>(conversationId: string, rpc: PullRequestRpcModule.PullRequestRpc) =>
    overRest(
      module,
      conversationId,
      rpc.method,
      rpc.call,
    ) as Promise<T>) as typeof module.writePullRequestRpc;
  return {
    ...module,
    readPullRequestRpc: read,
    writePullRequestRpc: write,
    readPullRequestViaWorkspaceApi: read,
    writePullRequestViaWorkspaceApi: write,
  };
}
