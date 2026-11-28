// A pull request read or write through the workspace API's methods, answered as the same JSON
// and errors the session's `/resources/github` REST route gives, so the pages show them alike.

import type { JsonObject } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";

import {
  callPullRequestMethod,
  callPullRequestWrite,
  type Method,
  servingRoutes,
  type WorkspaceClient,
  workspaceMethodName,
} from "@/lib/workspaceApi";
import { HostUnreachableError } from "@/lib/hostTransportRecovery";
import { RunnerOfflineError } from "@/lib/workspaceReadErrors";

/** A refused pull request call, with the HTTP status REST would have answered. */
export class PullRequestApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/** No route of the session serves the method, so nothing was read or sent. */
export class PullRequestMethodUnservedError extends PullRequestApiError {
  readonly method: Method;

  constructor(method: Method) {
    super("This session's host can't do this yet. Update the host, then try again.", 501);
    this.name = "PullRequestMethodUnservedError";
    this.method = method;
  }
}

// The REST status a refused workspace API call stands for, so the page shows it the same way.
const STATUS_FOR_CODE: Partial<Record<Code, number>> = {
  [Code.InvalidArgument]: 400,
  [Code.FailedPrecondition]: 400,
  [Code.PermissionDenied]: 403,
  [Code.NotFound]: 404,
  [Code.Unauthenticated]: 401,
};

/** A PR resource's workspace API method, called on the route that serves it. */
export interface PullRequestRpc {
  method: Method;
  call: (client: WorkspaceClient) => Promise<{ payload?: JsonObject }>;
}

/** A refused workspace API call as the {@link PullRequestApiError} REST would have given. */
export function asPullRequestApiError(error: unknown): unknown {
  if (!(error instanceof ConnectError)) return error;
  return new PullRequestApiError(error.rawMessage, STATUS_FOR_CODE[error.code] ?? 500);
}

/**
 * Read one PR resource through the workspace API's method, where the session's route
 * serves it to this caller, as the same JSON the REST route answers with.
 *
 * Resolves `null` when no route serves it. A refusal is thrown as the
 * {@link PullRequestApiError} REST would have given.
 */
export async function readPullRequestViaWorkspaceApi<T>(
  conversationId: string,
  method: Method,
  read: PullRequestRpc["call"],
  signal?: AbortSignal,
): Promise<T | null> {
  try {
    const payload = await callPullRequestMethod(
      conversationId,
      method,
      async (client) => (await read(client)).payload ?? {},
      { signal },
    );
    return payload as T | null;
  } catch (error) {
    throw asPullRequestApiError(error);
  }
}

/**
 * Send one PR write through the workspace API's method, at most once, where the
 * session's route serves it. Resolves `null` when nothing was sent; a write that may
 * have been sent throws `PullRequestMaybeSentError`.
 */
export async function writePullRequestViaWorkspaceApi<T>(
  conversationId: string,
  { method, call }: PullRequestRpc,
): Promise<T | null> {
  try {
    const payload = await callPullRequestWrite(
      conversationId,
      method,
      async (client) => (await call(client)).payload ?? {},
    );
    return payload as T | null;
  } catch (error) {
    throw asPullRequestApiError(error);
  }
}

/**
 * Why nothing was read or sent. A host that answers without `method` needs an update. When
 * no route answers, an unavailable host reads as its runner being offline, and anything
 * else, such as a network failure, as the host not being reached.
 */
async function unservedError(conversationId: string, method: Method): Promise<Error> {
  const { serving, failures } = await servingRoutes(conversationId);
  if (serving.length > 0) {
    return serving.some(({ served }) => served.has(workspaceMethodName(method)))
      ? new HostUnreachableError()
      : new PullRequestMethodUnservedError(method);
  }
  const failure = failures.at(-1);
  return failure instanceof ConnectError && failure.code === Code.Unavailable
    ? new RunnerOfflineError()
    : new HostUnreachableError(failure);
}

/** {@link readPullRequestViaWorkspaceApi}, for a page with no REST path: unserved is an error. */
export async function readPullRequestRpc<T>(
  conversationId: string,
  method: Method,
  read: PullRequestRpc["call"],
  signal?: AbortSignal,
): Promise<T> {
  const payload = await readPullRequestViaWorkspaceApi<T>(conversationId, method, read, signal);
  if (payload === null) throw await unservedError(conversationId, method);
  return payload;
}

/** {@link writePullRequestViaWorkspaceApi}, for a page with no REST path: unserved is an error. */
export async function writePullRequestRpc<T>(
  conversationId: string,
  rpc: PullRequestRpc,
): Promise<T> {
  const payload = await writePullRequestViaWorkspaceApi<T>(conversationId, rpc);
  if (payload === null) throw await unservedError(conversationId, rpc.method);
  return payload;
}
