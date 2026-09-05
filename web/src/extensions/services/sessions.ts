import type { InfiniteData, QueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { ConversationsPage } from "@/hooks/useConversations";
import { isConversationUnseen } from "@/hooks/useUnseenConversations";
import { getOptimisticTitle } from "@/lib/optimisticTitles";
import type { ExtensionSessionPage, ExtensionSessionSummary } from "../types";
import { isExtensionSessionPageWithinBudget } from "../rpc/validation";
import { ExtensionHostServiceError } from "./errors";

export const SESSION_PAGE_DEFAULT_LIMIT = 25;
export const SESSION_PAGE_MAX_LIMIT = 1_000;
export const SESSION_TITLE_MAX_LENGTH = 256;
export const SESSION_WORKSPACE_MAX_LENGTH = 512;
export const SESSION_READ_INTERVAL_MS = 100;
const SESSION_CURSOR_MAX_LENGTH = 256;
const SESSION_ID_MAX_LENGTH = 256;
const MAX_CURSOR_BUDGET_VALUE = "x".repeat(SESSION_CURSOR_MAX_LENGTH);
const STATUSES = new Set<ExtensionSessionSummary["status"]>([
  "idle",
  "running",
  "waiting",
  "failed",
]);

export class SessionReadLimiter {
  private tail: Promise<void> = Promise.resolve();
  private lastReadAt = 0;

  run<T>(signal: AbortSignal, operation: () => Promise<T>): Promise<T> {
    const execute = async () => {
      if (signal.aborted) throw new DOMException("Host operation cancelled", "AbortError");
      const waitMs = Math.max(0, SESSION_READ_INTERVAL_MS - (Date.now() - this.lastReadAt));
      if (waitMs > 0) {
        await new Promise<void>((resolve) => {
          setTimeout(resolve, waitMs);
        });
      }
      if (signal.aborted) throw new DOMException("Host operation cancelled", "AbortError");
      const result = await operation();
      this.lastReadAt = Date.now();
      return result;
    };
    const result = this.tail.then(execute, execute);
    this.tail = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }
}

interface SessionPageRequest {
  after: string | null;
  limit: number;
}

function plainObject(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

export function parseSessionPageRequest(params: unknown): SessionPageRequest {
  const value = plainObject(params);
  if (!value) throw new ExtensionHostServiceError("InvalidParams", "Expected an object");
  const afterValue = value.after;
  let after: string | null = null;
  if (afterValue !== undefined && afterValue !== null) {
    if (
      typeof afterValue !== "string" ||
      afterValue.length < 1 ||
      afterValue.length > SESSION_CURSOR_MAX_LENGTH
    ) {
      throw new ExtensionHostServiceError("InvalidParams", "after cursor is invalid");
    }
    after = afterValue;
  }
  const limitValue = value.limit ?? SESSION_PAGE_DEFAULT_LIMIT;
  if (
    typeof limitValue !== "number" ||
    !Number.isInteger(limitValue) ||
    limitValue < 1 ||
    limitValue > SESSION_PAGE_MAX_LIMIT
  ) {
    throw new ExtensionHostServiceError(
      "InvalidParams",
      `limit must be an integer from 1 to ${SESSION_PAGE_MAX_LIMIT}`,
    );
  }
  return { after, limit: limitValue };
}

export function sessionListQuery(request: SessionPageRequest): string {
  const query = new URLSearchParams();
  query.set("limit", String(request.limit));
  query.set("sort_by", "updated_at");
  query.set("order", "desc");
  query.set("kind", "default");
  query.set("include_archived", "false");
  if (request.after) query.set("after", request.after);
  return query.toString();
}

function optionalBoundedString(value: unknown, field: string, maximum: number): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "string") {
    throw new ExtensionHostServiceError("HostError", `Session ${field} is malformed`);
  }
  return value.slice(0, maximum);
}

function projectSession(value: unknown): ExtensionSessionSummary {
  const row = plainObject(value);
  if (!row) throw new ExtensionHostServiceError("HostError", "Session row is malformed");
  const { id, status, created_at: createdAt, updated_at: updatedAt } = row;
  if (typeof id !== "string" || id.length < 1 || id.length > SESSION_ID_MAX_LENGTH) {
    throw new ExtensionHostServiceError("HostError", "Session id is malformed");
  }
  if (typeof status !== "string" || !STATUSES.has(status as ExtensionSessionSummary["status"])) {
    throw new ExtensionHostServiceError("HostError", "Session status is malformed");
  }
  if (typeof createdAt !== "number" || !Number.isFinite(createdAt)) {
    throw new ExtensionHostServiceError("HostError", "Session created_at is malformed");
  }
  if (typeof updatedAt !== "number" || !Number.isFinite(updatedAt)) {
    throw new ExtensionHostServiceError("HostError", "Session updated_at is malformed");
  }
  const title = optionalBoundedString(row.title, "title", SESSION_TITLE_MAX_LENGTH);
  // Same provisional label the sidebar shows before the server seeds a title.
  const optimisticTitle = title === null ? getOptimisticTitle(id) : undefined;
  return {
    id,
    title: title ?? optimisticTitle?.slice(0, SESSION_TITLE_MAX_LENGTH) ?? null,
    titleProvisional: title === null && optimisticTitle !== undefined,
    status: status as ExtensionSessionSummary["status"],
    unread: isConversationUnseen(id, updatedAt, status),
    workspace: optionalBoundedString(row.workspace, "workspace", SESSION_WORKSPACE_MAX_LENGTH),
    gitBranch: optionalBoundedString(row.git_branch, "git_branch", SESSION_TITLE_MAX_LENGTH),
    projectId: optionalBoundedString(row.project_id, "project_id", SESSION_ID_MAX_LENGTH),
    createdAt,
    updatedAt,
  };
}

export function cachedInitialSessionPage(
  queryClient: QueryClient,
  params: unknown,
): ExtensionSessionPage | null {
  const request = parseSessionPageRequest(params);
  if (request.after !== null) return null;
  const cached = queryClient.getQueryData<InfiniteData<ConversationsPage, string | undefined>>([
    "conversations",
    "",
    true,
  ]);
  if (!cached || cached.pages.length === 0) return null;
  const rows = cached.pages
    .flatMap((page) => page.data)
    .filter((session) => session.archived !== true && session.parent_session_id == null);
  const selected = rows.slice(0, request.limit);
  const cacheHasMore = rows.length > request.limit || cached.pages.at(-1)?.has_more === true;
  if (cacheHasMore && selected.length === 0) return null;
  try {
    return projectSessionPage(
      {
        data: selected,
        has_more: cacheHasMore,
        last_id: cacheHasMore ? selected.at(-1)?.id : null,
      },
      request.limit,
    );
  } catch {
    return null;
  }
}

export function projectSessionPage(payload: unknown, limit: number): ExtensionSessionPage {
  const page = plainObject(payload);
  if (!page || !Array.isArray(page.data) || page.data.length > limit) {
    throw new ExtensionHostServiceError("HostError", "Session list response is malformed");
  }
  if (typeof page.has_more !== "boolean") {
    throw new ExtensionHostServiceError("HostError", "Session pagination is malformed");
  }
  if (
    page.last_id !== undefined &&
    page.last_id !== null &&
    (typeof page.last_id !== "string" ||
      page.last_id.length < 1 ||
      page.last_id.length > SESSION_CURSOR_MAX_LENGTH)
  ) {
    throw new ExtensionHostServiceError("HostError", "Session cursor is malformed");
  }
  const projected = page.data.map(projectSession);
  const serverHasMore = page.has_more;
  const serverCursor = typeof page.last_id === "string" ? page.last_id : null;
  const resultFor = (count: number): ExtensionSessionPage => {
    const sessions = projected.slice(0, count);
    const truncated = count < projected.length;
    const hasMore = truncated || serverHasMore;
    const nextCursor = hasMore
      ? truncated
        ? (sessions.at(-1)?.id ?? null)
        : (serverCursor ?? sessions.at(-1)?.id ?? null)
      : null;
    return { sessions, nextCursor, hasMore };
  };

  if (projected.length === 0) {
    const result = resultFor(0);
    if (result.hasMore && !result.nextCursor) {
      throw new ExtensionHostServiceError("HostError", "Session list has more pages but no cursor");
    }
    return result;
  }

  const fullResult = resultFor(projected.length);
  if (isExtensionSessionPageWithinBudget(fullResult)) return fullResult;

  let result: ExtensionSessionPage | null = null;
  let lower = 1;
  let upper = projected.length - 1;
  while (lower <= upper) {
    const count = Math.floor((lower + upper) / 2);
    const candidate = resultFor(count);
    const budgetCandidate = { ...candidate, nextCursor: MAX_CURSOR_BUDGET_VALUE };
    if (isExtensionSessionPageWithinBudget(budgetCandidate)) {
      result = candidate;
      lower = count + 1;
    } else {
      upper = count - 1;
    }
  }
  if (!result) {
    throw new ExtensionHostServiceError("HostError", "Session page exceeds the response budget");
  }
  if (result.hasMore && !result.nextCursor) {
    throw new ExtensionHostServiceError("HostError", "Session list has more pages but no cursor");
  }
  return result;
}

export async function listSessionPage(
  params: unknown,
  signal: AbortSignal,
): Promise<ExtensionSessionPage> {
  if (signal.aborted) throw new DOMException("Host operation cancelled", "AbortError");
  const request = parseSessionPageRequest(params);
  let response: Response;
  try {
    response = await authenticatedFetch(`/v1/sessions?${sessionListQuery(request)}`, {
      signal,
    });
  } catch (error) {
    if (signal.aborted || (error instanceof DOMException && error.name === "AbortError")) {
      throw error;
    }
    throw new ExtensionHostServiceError("HostError", "Session list request failed");
  }
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new ExtensionHostServiceError("PermissionDenied", "Session list access was denied");
    }
    if (response.status >= 500) {
      throw new ExtensionHostServiceError("Unavailable", "Session list is unavailable");
    }
    throw new ExtensionHostServiceError(
      "HostError",
      `Session list request failed (${response.status})`,
    );
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch (error) {
    if (signal.aborted) throw error;
    throw new ExtensionHostServiceError("HostError", "Malformed session list response");
  }
  return projectSessionPage(payload, request.limit);
}
