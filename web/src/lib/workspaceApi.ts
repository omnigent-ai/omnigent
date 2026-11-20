// Client for the workspace API (`omnigent.api.workspace.v1alpha1`), which reads
// and searches a session's workspace files through its host; see viaWorkspaceApi.

import { Code, ConnectError, createClient, type Client } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { base64Encode } from "@bufbuild/protobuf/wire";
import type { MessageInitShape } from "@bufbuild/protobuf";
import {
  ContentEncoding,
  EntryType,
  FileSearchMode,
  WorkspaceService,
  type ComparisonSchema,
  type Entry,
  type FileMetadata,
  type ListBranchCommitsResponse,
  type ListRepositoryRefsResponse,
  type ListWorkingCopyChangesResponse,
  type ResolveRepositoryContextResponse,
  type WorkingCopyBase,
} from "@/gen/omnigent/api/workspace/v1alpha1/workspace_pb";
import type { FileContentResponse } from "@/hooks/useFileContent";
import type { ContentSearchResult } from "@/lib/contentSearch";
import { getCachedServerInfo, isFeatureEnabled } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { splitGlobList } from "@/lib/globList";
import { workspaceSocketBridge, type WorkspaceSocketRequest } from "@/lib/nativeBridge";

type WorkspaceClient = Client<typeof WorkspaceService>;
type Route = "direct" | "server";
type Method = keyof typeof WorkspaceService.method;
/** What a Git comparison compares; see `Comparison` in workspace.proto. */
export type ComparisonInit = MessageInitShape<typeof ComparisonSchema>;

/** The server's grant to read one session's workspace from its host's socket. */
interface DirectGrant {
  hostId: string;
  socketPath: string;
  token: string;
  /** When the host stops accepting the token, in this machine's clock (the host's). */
  expiresAtMs: number;
}

/** A session's grant request, and the grant once it arrives. */
interface GrantEntry {
  pending: Promise<DirectGrant>;
  grant?: DirectGrant;
}

// Renew a grant this long before the host would refuse it.
const GRANT_RENEW_MARGIN_MS = 30_000;
// A grant request that takes longer is abandoned, so the next read asks again.
const GRANT_TIMEOUT_MS = 10_000;
// A route that failed for a session is skipped for this long, then tried again.
const ROUTE_RETRY_MS = 5 * 60_000;
// Answers about the request itself, not failures of the route that carried it.
const ANSWER_CODES: ReadonlySet<Code> = new Set([Code.NotFound, Code.InvalidArgument]);
// Also answers for reads refused on the file's current state, e.g. a moved base.
const FILE_STATE_ANSWER_CODES: ReadonlySet<Code> = new Set([
  ...ANSWER_CODES,
  Code.FailedPrecondition,
]);
// The host is briefly away, e.g. reconnecting: try the next route, keep this one.
const TRANSIENT_CODES = new Set([Code.Unavailable]);
// Every host with the workspace API serves these, so they need no capability check.
const BASELINE_METHODS = new Set<Method>(["listDirectory", "readFile"]);
// A capability check that takes longer is abandoned, failing the route.
const CAPABILITIES_TIMEOUT_MS = 10_000;

/** A route's answer to GetCapabilities, shared by the calls that wait for it. */
interface CapabilitiesEntry {
  pending: Promise<ReadonlySet<string>>;
  expiresAtMs: number;
}

const grants = new Map<string, GrantEntry>();
const failedUntil = new Map<string, number>();
// Kept apart from grants, which a failed direct call drops.
const capabilities = new Map<string, CapabilitiesEntry>();
const unsupportedUntil = new Map<string, number>();

/** A failed direct route is retried as soon as a read brings an SSH alias it didn't have. */
/**
 * A method's answer the client can't use, e.g. a stream cut short: the route
 * skips that method for a while and keeps serving the rest.
 */
class UnusableAnswerError extends Error {}

function routeKey(route: Route, sessionId: string, sshTarget?: string | null): string {
  return route === "direct" ? `direct:${sessionId}:${sshTarget ?? ""}` : `${route}:${sessionId}`;
}

function routesFor(sessionId: string, sshTarget?: string | null): Route[] {
  const info = getCachedServerInfo() ?? "loading";
  const routes: Route[] = [];
  if (workspaceSocketBridge() && isFeatureEnabled(info, "workspace_direct_access")) {
    routes.push("direct");
  }
  if (isFeatureEnabled(info, "workspace_api_server_access")) routes.push("server");
  return routes.filter(
    (route) => (failedUntil.get(routeKey(route, sessionId, sshTarget)) ?? 0) <= Date.now(),
  );
}

function sessionPath(sessionId: string): string {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/workspace`;
}

/** Settle with `promise`, or reject as soon as `signal` aborts. */
function abortable<T>(promise: Promise<T>, signal?: AbortSignal | null): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason);
    signal.addEventListener("abort", onAbort, { once: true });
    void promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", onAbort));
  });
}

async function requestGrant(sessionId: string): Promise<DirectGrant> {
  const res = await authenticatedFetch(`${sessionPath(sessionId)}/direct-access`, {
    method: "POST",
    signal: AbortSignal.timeout(GRANT_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as {
    host_id: string;
    socket_path: string;
    token: string;
    expires_at: number;
  };
  return {
    hostId: body.host_id,
    socketPath: body.socket_path,
    token: body.token,
    expiresAtMs: body.expires_at * 1000,
  };
}

/** The session's current grant; concurrent reads share one request for it. */
function directGrant(sessionId: string): Promise<DirectGrant> {
  const cached = grants.get(sessionId);
  if (cached && (!cached.grant || cached.grant.expiresAtMs - GRANT_RENEW_MARGIN_MS > Date.now())) {
    return cached.pending;
  }
  const entry: GrantEntry = { pending: requestGrant(sessionId) };
  entry.pending.then(
    (grant) => {
      entry.grant = grant;
    },
    () => {
      if (grants.get(sessionId) === entry) grants.delete(sessionId);
    },
  );
  grants.set(sessionId, entry);
  return entry.pending;
}

function directClient(
  grant: DirectGrant,
  send: (request: WorkspaceSocketRequest) => Promise<{
    status: number;
    headers: Record<string, string>;
    body: Uint8Array;
  }>,
  sshTarget?: string | null,
): WorkspaceClient {
  return createClient(
    WorkspaceService,
    createConnectTransport({
      // Only the path reaches the socket; the shell picks the socket.
      baseUrl: "http://workspace.socket",
      useBinaryFormat: true,
      fetch: async (input, init) => {
        const headers = Object.fromEntries(new Headers(init?.headers).entries());
        headers.authorization = `Bearer ${grant.token}`;
        // The shell can't cancel its call, so a cancelled read just stops waiting.
        const response = await abortable(
          send({
            hostId: grant.hostId,
            ...(sshTarget ? { sshTarget } : {}),
            socketPath: grant.socketPath,
            path: new URL(String(input)).pathname,
            headers,
            body: (init?.body as Uint8Array | undefined) ?? new Uint8Array(),
          }),
          init?.signal,
        );
        return new Response(response.body as Uint8Array<ArrayBuffer>, {
          status: response.status,
          headers: response.headers,
        });
      },
    }),
  );
}

function serverClient(sessionId: string): WorkspaceClient {
  return createClient(
    WorkspaceService,
    createConnectTransport({
      baseUrl: sessionPath(sessionId),
      useBinaryFormat: true,
      // Session-scoped, so it rides the host's routing key like the file routes.
      fetch: (input, init) => authenticatedFetch(input, init),
    }),
  );
}

async function callOn<T>(
  route: Route,
  sessionId: string,
  call: (client: WorkspaceClient) => Promise<T>,
  signal?: AbortSignal,
  sshTarget?: string | null,
  answerCodes: ReadonlySet<Code> = ANSWER_CODES,
): Promise<T> {
  if (route === "server") return call(serverClient(sessionId));
  const send = workspaceSocketBridge();
  if (!send) throw new Error("The desktop shell can't reach local sockets");
  const pending = directGrant(sessionId);
  try {
    const grant = await abortable(pending, signal);
    return await call(directClient(grant, send, sshTarget));
  } catch (error) {
    // The host accepted the token if it answered about the request or method.
    const answered =
      error instanceof ConnectError &&
      (answerCodes.has(error.code) || error.code === Code.Unimplemented);
    // Drop only the grant this read used; a parallel read may have renewed it.
    if (!answered && !signal?.aborted && grants.get(sessionId)?.pending === pending) {
      grants.delete(sessionId);
    }
    throw error;
  }
}

/** The proto's method name, e.g. "SearchFiles", as GetCapabilities lists it. */
function capabilityName(method: Method): string {
  return WorkspaceService.method[method].name;
}

/**
 * The methods a route's host serves. Concurrent calls share one check, which
 * runs on its own deadline so that one caller cancelling can't fail it for the rest.
 */
function routeCapabilities(
  route: Route,
  sessionId: string,
  sshTarget?: string | null,
): Promise<ReadonlySet<string>> {
  const key = routeKey(route, sessionId, sshTarget);
  const cached = capabilities.get(key);
  if (cached && cached.expiresAtMs > Date.now()) return cached.pending;
  const entry: CapabilitiesEntry = {
    pending: callOn(
      route,
      sessionId,
      async (client) => {
        const controller = new AbortController();
        const deadline = setTimeout(() => controller.abort(), CAPABILITIES_TIMEOUT_MS);
        try {
          const response = await client.getCapabilities({}, { signal: controller.signal });
          return new Set(response.capabilities);
        } catch (error) {
          // A host without the capability query serves only the baseline methods.
          if (error instanceof ConnectError && error.code === Code.Unimplemented) {
            return new Set<string>();
          }
          throw error;
        } finally {
          clearTimeout(deadline);
        }
      },
      undefined,
      sshTarget,
    ),
    expiresAtMs: Date.now() + ROUTE_RETRY_MS,
  };
  entry.pending.catch(() => {
    if (capabilities.get(key) === entry) capabilities.delete(key);
  });
  capabilities.set(key, entry);
  return entry.pending;
}

function concat(chunks: Uint8Array[]): Uint8Array {
  const bytes = new Uint8Array(chunks.reduce((total, chunk) => total + chunk.length, 0));
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return bytes;
}

/** A file stream's messages: metadata first, then the bytes in order. */
type FileMessages = AsyncIterable<{ metadata?: FileMetadata; data: Uint8Array }>;

/** Collect a ReadFile or ReadBaseFile stream into the REST file payload. */
async function collectFile(path: string, messages: FileMessages): Promise<FileContentResponse> {
  let metadata: FileMetadata | undefined;
  const chunks: Uint8Array[] = [];
  for await (const message of messages) {
    metadata ??= message.metadata;
    chunks.push(message.data);
  }
  if (!metadata) throw new ConnectError("The workspace API sent no file metadata", Code.Internal);
  const bytes = concat(chunks);
  // The same payload the REST route sends, so every viewer reads it unchanged.
  const text = metadata.encoding === ContentEncoding.UTF8;
  return {
    object: "session.environment.filesystem.file_content",
    path,
    content_type: metadata.contentType || null,
    encoding: text ? "utf-8" : "base64",
    // Keep a leading BOM, as REST does.
    content: text
      ? new TextDecoder("utf-8", { ignoreBOM: true }).decode(bytes)
      : base64Encode(bytes),
    bytes: Number(metadata.size),
    truncated: metadata.truncated,
  };
}

/** One directory entry, in the REST listing's shape. */
export interface ListedEntry {
  id: string;
  name: string;
  path: string;
  type: "file" | "directory";
  bytes: number | null;
  modified_at: number | null;
}

const LIST_PAGE_SIZE = 1000;

async function listDirectory(
  client: WorkspaceClient,
  path: string,
  maxEntries: number,
  signal?: AbortSignal,
): Promise<{ data: ListedEntry[]; hasMore: boolean }> {
  const data: ListedEntry[] = [];
  let pageToken = "";
  // Each page needs the token the previous one returned.
  /* oxlint-disable no-await-in-loop */
  do {
    const page = await client.listDirectory(
      { path, pageSize: Math.min(LIST_PAGE_SIZE, maxEntries - data.length), pageToken },
      { signal },
    );
    // A host that hands back the token it was given is resending a page; stop before keeping it.
    if (pageToken && page.nextPageToken === pageToken) break;
    data.push(...page.entries.map(listedEntry));
    pageToken = page.nextPageToken;
    // A page that adds nothing would never end the listing; report the rest as more.
    if (page.entries.length === 0) break;
  } while (pageToken && data.length < maxEntries);
  /* oxlint-enable no-await-in-loop */
  return { data, hasMore: pageToken !== "" };
}

interface CallOptions {
  signal?: AbortSignal;
  sshTarget?: string | null;
  /** Optional features of `method` the call relies on, e.g. "search_files.substring". */
  features?: readonly string[];
  /** Codes that answer the request rather than fail the route. */
  answerCodes?: ReadonlySet<Code>;
  /** Throw an answer to the caller, for methods REST can't answer instead. */
  throwAnswers?: boolean;
}

/**
 * Call the workspace API on the first route that works, or return null so the
 * caller uses REST.
 *
 * Tries the host's local socket first (desktop shell only, with a short-lived
 * token the server grants the session's owner), then the server. A route that
 * fails is skipped for the session for a while. A route whose host doesn't
 * serve `method`, or gives an answer the client can't use, is skipped for that
 * method only, so the host keeps serving the rest. An error answer also
 * returns null, since REST reports it in the shape callers expect, unless
 * `throwAnswers` is set.
 */
async function viaWorkspaceApi<T>(
  sessionId: string,
  method: Method,
  call: (client: WorkspaceClient) => Promise<T>,
  {
    signal,
    sshTarget,
    features = [],
    answerCodes = ANSWER_CODES,
    throwAnswers = false,
  }: CallOptions = {},
): Promise<T | null> {
  const checked = !BASELINE_METHODS.has(method) || features.length > 0;
  // Each route's attempt must finish before the next is tried.
  /* oxlint-disable no-await-in-loop */
  for (const route of routesFor(sessionId, sshTarget)) {
    const key = routeKey(route, sessionId, sshTarget);
    const unsupportedKey = `${key}:${method}`;
    if ((unsupportedUntil.get(unsupportedKey) ?? 0) > Date.now()) continue;
    try {
      if (checked) {
        const served = await abortable(routeCapabilities(route, sessionId, sshTarget), signal);
        const required = [capabilityName(method), ...features];
        if (!required.every((name) => served.has(name))) continue;
      }
      return await callOn(route, sessionId, call, signal, sshTarget, answerCodes);
    } catch (error) {
      signal?.throwIfAborted();
      if (error instanceof UnusableAnswerError) {
        unsupportedUntil.set(unsupportedKey, Date.now() + ROUTE_RETRY_MS);
        continue;
      }
      const code = error instanceof ConnectError ? error.code : null;
      if (code !== null && answerCodes.has(code)) {
        if (throwAnswers) throw error;
        return null;
      }
      if (code !== null && TRANSIENT_CODES.has(code)) continue;
      // A restarted host refuses the old token; the next read gets a new one.
      if (route === "direct" && code === Code.Unauthenticated) {
        // A restarted host may serve other methods, so its capabilities are read again.
        capabilities.delete(key);
        continue;
      }
      // The host listed a method it doesn't serve, e.g. one the server turned off.
      if (checked && code === Code.Unimplemented) {
        unsupportedUntil.set(unsupportedKey, Date.now() + ROUTE_RETRY_MS);
        continue;
      }
      failedUntil.set(key, Date.now() + ROUTE_RETRY_MS);
    }
  }
  /* oxlint-enable no-await-in-loop */
  return null;
}

/**
 * List a workspace directory through the workspace API, in the REST listing's
 * shape and order (ascending by path), reading pages up to `maxEntries`.
 *
 * :param sessionId: The session whose workspace to list.
 * :param path: Workspace-relative directory, ``""`` for the root.
 * :param maxEntries: Stop after this many entries, e.g. ``1000``; must be positive.
 * :returns: The entries and whether more remain, or null when the caller
 *     should list through REST (see :func:`readFileViaWorkspaceApi`).
 */
export function listDirectoryViaWorkspaceApi(
  sessionId: string,
  path: string,
  maxEntries: number,
  signal?: AbortSignal,
): Promise<{ data: ListedEntry[]; hasMore: boolean } | null> {
  // The host reads a page size of 0 as its default, so a 0 limit would still return entries.
  if (!(maxEntries > 0)) return Promise.reject(new RangeError("maxEntries must be positive"));
  return viaWorkspaceApi(
    sessionId,
    "listDirectory",
    (client) => listDirectory(client, path, maxEntries, signal),
    { signal },
  );
}

/**
 * Read a workspace file through the workspace API, on the first route that
 * works (see :func:`viaWorkspaceApi`).
 *
 * :param sessionId: The session whose workspace to read.
 * :param path: Workspace-relative path, e.g. ``"src/main.py"``.
 * :param sshTarget: An SSH alias the desktop may try for a host on another
 *     machine it has no destination for, e.g. from the host's provider.
 * :returns: The REST route's payload, or null when the caller should read
 *     through REST: no route is enabled or working, or the API answered with
 *     an error.
 */
export async function readFileViaWorkspaceApi(
  sessionId: string,
  path: string,
  signal?: AbortSignal,
  sshTarget?: string | null,
): Promise<FileContentResponse | null> {
  return viaWorkspaceApi(
    sessionId,
    "readFile",
    (client) => collectFile(path, client.readFile({ path }, { signal })),
    { signal, sshTarget },
  );
}

function listedEntry(entry: Entry): ListedEntry {
  return {
    // The REST listing's entry id is its path.
    id: entry.path,
    name: entry.name,
    path: entry.path,
    type: entry.type === EntryType.DIRECTORY ? "directory" : "file",
    bytes: entry.size === undefined ? null : Number(entry.size),
    modified_at: entry.modifyTime ? Number(entry.modifyTime.seconds) : null,
  };
}

/** File search matches, in the REST search's shape. */
export interface FoundEntries {
  /** Paths relative to the searched directory, as the REST search reports them. */
  data: ListedEntry[];
  /** The search returned its limit, so more entries may match. */
  hasMore: boolean;
  /** Some files weren't searched, so a missing file may still exist. */
  truncated: boolean;
}

export interface FileSearchOptions {
  /** Workspace-relative directory to search under, ``""`` for the root. */
  path: string;
  /** Comma-separated globs, e.g. ``"*.{js,ts}, src/**"``. */
  include: string;
  exclude: string;
  /** Maximum entries, 1 to 500. */
  limit: number;
}

/**
 * Search workspace file and directory paths through the workspace API, with
 * the REST ``/search`` route's case-insensitive substring match.
 *
 * :param query: Substring of an entry's path; not blank.
 * :returns: The matches, or null when the caller should search through REST:
 *     see :func:`viaWorkspaceApi`, or the host's file list is still being built.
 */
export function searchFilesViaWorkspaceApi(
  sessionId: string,
  query: string,
  { path, include, exclude, limit }: FileSearchOptions,
  signal?: AbortSignal,
): Promise<FoundEntries | null> {
  return viaWorkspaceApi(
    sessionId,
    "searchFiles",
    async (client) => {
      const response = await client.searchFiles(
        {
          query,
          mode: FileSearchMode.SUBSTRING,
          maximumResults: limit,
          path,
          include: splitGlobList(include),
          exclude: splitGlobList(exclude),
          // The REST search walks ignored files too, so they stay findable.
          includeGitIgnored: true,
        },
        { signal },
      );
      // The file list is still being built: REST answers now instead of a partial list.
      if (response.refreshing) return null;
      return {
        data: response.results.flatMap((result) =>
          result.entry ? [listedEntry(result.entry)] : [],
        ),
        hasMore: response.truncated,
        truncated: response.indexIncomplete || response.untrackedUnavailable,
      };
    },
    { signal, features: ["search_files.substring"] },
  );
}

export interface TextSearchOptions {
  caseSensitive: boolean;
  /** Read the query as a ripgrep regular expression. */
  regex: boolean;
  /** Comma-separated globs; an include also searches files ignore rules skip. */
  include: string;
  exclude: string;
}

/**
 * Search workspace file contents through the workspace API, in the REST
 * content search's shape. The host streams matches; this collects them.
 *
 * :param query: One line, 1 to 4096 characters, and not blank.
 * :returns: The matches, or null when the caller should search through REST:
 *     content search is off, no route serves it, or the search is invalid,
 *     which REST reports with its own message.
 */
export function searchTextViaWorkspaceApi(
  sessionId: string,
  query: string,
  { caseSensitive, regex, include, exclude }: TextSearchOptions,
  signal?: AbortSignal,
): Promise<ContentSearchResult | null> {
  const info = getCachedServerInfo() ?? "loading";
  // The direct route skips the server, so the server's switch is checked here.
  if (!isFeatureEnabled(info, "workspace_content_search")) return Promise.resolve(null);
  return viaWorkspaceApi(
    sessionId,
    "searchText",
    async (client) => {
      const result: ContentSearchResult = { matches: [], truncated: false, warning: null };
      let done = false;
      const stream = client.searchText(
        { query, caseSensitive, regularExpression: regex, include, exclude },
        { signal },
      );
      for await (const message of stream) {
        for (const match of message.matches) {
          result.matches.push({
            path: match.path,
            line: Number(match.line),
            column: match.column,
            text: match.text,
            match_start: match.matchStart,
            match_end: match.matchEnd,
          });
        }
        if (message.done) {
          done = true;
          result.truncated = message.truncated;
          result.warning = message.warning || null;
        }
      }
      // A stream cut short would pass for a complete answer.
      if (!done) throw new UnusableAnswerError("The content search ended early");
      return result;
    },
    { signal },
  );
}

// Git comparisons. Each returns null when no route serves the method, so the
// caller can say the comparison is unavailable. REST can't answer instead, so a
// request the host rejects throws its ConnectError, whose message says why.
const GIT_OPTIONS = { throwAnswers: true } as const;

/** Resolve a comparison's base and head commits. */
export function resolveWorkingCopyBaseViaWorkspaceApi(
  sessionId: string,
  comparison: ComparisonInit,
  signal?: AbortSignal,
): Promise<WorkingCopyBase | null> {
  return viaWorkspaceApi(
    sessionId,
    "resolveWorkingCopyBase",
    async (client) => {
      const response = await client.resolveWorkingCopyBase({ comparison }, { signal });
      if (!response.base) throw new UnusableAnswerError("The workspace API sent no base");
      return response.base;
    },
    { ...GIT_OPTIONS, signal },
  );
}

/** List the files a comparison changes, with its resolved base. */
export function listWorkingCopyChangesViaWorkspaceApi(
  sessionId: string,
  comparison: ComparisonInit,
  signal?: AbortSignal,
): Promise<ListWorkingCopyChangesResponse | null> {
  return viaWorkspaceApi(
    sessionId,
    "listWorkingCopyChanges",
    (client) => client.listWorkingCopyChanges({ comparison }, { signal }),
    { ...GIT_OPTIONS, signal },
  );
}

/**
 * Read a file's content at a commit, in the REST file payload's shape.
 *
 * :param file: The commit, path and blob, e.g. a ChangedFile's ``base_object``.
 * :raises ConnectError: FailedPrecondition when the path no longer has that
 *     blob, e.g. the base moved since the changes were listed.
 */
export function readBaseFileViaWorkspaceApi(
  sessionId: string,
  file: { commit: string; path: string; object: string },
  signal?: AbortSignal,
): Promise<FileContentResponse | null> {
  return viaWorkspaceApi(
    sessionId,
    "readBaseFile",
    (client) => collectFile(file.path, client.readBaseFile(file, { signal })),
    { ...GIT_OPTIONS, signal, answerCodes: FILE_STATE_ANSWER_CODES },
  );
}

/**
 * Read a symlink's own text, without following it.
 *
 * :raises ConnectError: FailedPrecondition when the path isn't a symlink.
 */
export function readLinkTextViaWorkspaceApi(
  sessionId: string,
  path: string,
  signal?: AbortSignal,
): Promise<Uint8Array | null> {
  return viaWorkspaceApi(
    sessionId,
    "readLinkText",
    async (client) => (await client.readLinkText({ path }, { signal })).target,
    { ...GIT_OPTIONS, signal, answerCodes: FILE_STATE_ANSWER_CODES },
  );
}

/** Branch and tag names to offer as a comparison's base or head. */
export function listRepositoryRefsViaWorkspaceApi(
  sessionId: string,
  signal?: AbortSignal,
): Promise<ListRepositoryRefsResponse | null> {
  return viaWorkspaceApi(
    sessionId,
    "listRepositoryRefs",
    (client) => client.listRepositoryRefs({}, { signal }),
    { ...GIT_OPTIONS, signal },
  );
}

/** The commits HEAD has beyond its merge base with `baseRevision`, newest first. */
export function listBranchCommitsViaWorkspaceApi(
  sessionId: string,
  baseRevision: string,
  signal?: AbortSignal,
): Promise<ListBranchCommitsResponse | null> {
  return viaWorkspaceApi(
    sessionId,
    "listBranchCommits",
    (client) => client.listBranchCommits({ baseRevision }, { signal }),
    { ...GIT_OPTIONS, signal },
  );
}

/** The checked-out branch, where it pushes, and the remotes. */
export function resolveRepositoryContextViaWorkspaceApi(
  sessionId: string,
  signal?: AbortSignal,
): Promise<ResolveRepositoryContextResponse | null> {
  return viaWorkspaceApi(
    sessionId,
    "resolveRepositoryContext",
    (client) => client.resolveRepositoryContext({}, { signal }),
    { ...GIT_OPTIONS, signal },
  );
}
