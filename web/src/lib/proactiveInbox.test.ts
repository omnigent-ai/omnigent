import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import {
  applySuggestionUpdate,
  listSuggestions,
  reconcileSuggestion,
  stateAfterUpdate,
  suggestionLinkTarget,
  suggestionPreview,
  unreadCountText,
  updateSuggestion,
  type Suggestion,
  type SuggestionPage,
} from "./proactiveInbox";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

const mockFetch = vi.mocked(authenticatedFetch);

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const wire = {
  id: "sug_1",
  kind: "followup",
  title: "Ramp up",
  body_md: "Body",
  links: [{ type: "pr", ref: "https://github.com/acme/app/pull/7" }],
  actions: [{ label: "Ramp up", prompt: "Ramp it" }, { label: 3 }],
  chart: { version: 1 },
  source_session_id: "sess",
  state: "unread",
  resolution: "none",
  created_at: 10,
};

function item(overrides: Partial<Suggestion> = {}): Suggestion {
  return {
    id: "a",
    kind: "info",
    title: "t",
    bodyMd: "b",
    links: [],
    actions: [],
    chart: null,
    sourceSessionId: "s",
    state: "unread",
    resolution: "none",
    createdAt: 1,
    ...overrides,
  };
}

function page(data: Suggestion[], overrides: Partial<SuggestionPage> = {}): SuggestionPage {
  return { data, nextCursor: null, unreadCount: 1, unreadCountCapped: false, ...overrides };
}

describe("API calls", () => {
  beforeEach(() => mockFetch.mockReset());

  it("lists one page of open suggestions with the cursor and unread count", async () => {
    mockFetch.mockResolvedValue(
      response({
        data: [wire],
        next_cursor: "10:abc",
        unread_count: 100,
        unread_count_capped: true,
      }),
    );

    const result = await listSuggestions(null);

    expect(mockFetch.mock.calls[0][0]).toBe("/v1/inbox/items?limit=50&state=unread&state=read");
    expect(result).toMatchObject({
      nextCursor: "10:abc",
      unreadCount: 100,
      unreadCountCapped: true,
    });
    expect(result.data[0]).toMatchObject({ id: "sug_1", bodyMd: "Body", sourceSessionId: "sess" });
    expect(result.data[0].actions).toEqual([{ label: "Ramp up", prompt: "Ramp it" }]);
  });

  it("asks for the next page of one state", async () => {
    mockFetch.mockResolvedValue(response({ data: [], next_cursor: null, unread_count: 0 }));

    const result = await listSuggestions("10:abc", ["unread"]);

    expect(mockFetch.mock.calls[0][0]).toBe("/v1/inbox/items?limit=50&state=unread&after=10%3Aabc");
    expect(result.unreadCountCapped).toBe(false);
  });

  it.each([
    [{ state: "read" as const }, { state: "read" }],
    [
      { resolution: "action_taken" as const, resolutionAction: 1 },
      { resolution: "action_taken", resolution_action: 1 },
    ],
    [{ resolution: "not_now" as const }, { resolution: "not_now" }],
  ])("patches %j as %j", async (update, body) => {
    mockFetch.mockResolvedValue(response({ ...wire, state: "done" }));

    await updateSuggestion("sug_1", update);

    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe("/v1/inbox/items/sug_1");
    expect(init).toMatchObject({ method: "PATCH", body: JSON.stringify(body) });
  });
});

describe("applySuggestionUpdate", () => {
  const pages = [page([item({ id: "a" })]), page([item({ id: "b", state: "read" })])];

  it("counts a read on every page and keeps the item listed", () => {
    const next = applySuggestionUpdate(pages, "a", { state: "read" });
    expect(next.map((p) => p.unreadCount)).toEqual([0, 0]);
    expect(next[0].data[0].state).toBe("read");
  });

  it("drops resolved items", () => {
    expect(applySuggestionUpdate(pages, "b", { resolution: "already_done" })[1].data).toEqual([]);
    const next = applySuggestionUpdate(pages, "a", {
      resolution: "action_taken",
      resolutionAction: 0,
    });
    expect(next[0].data).toEqual([]);
    expect(next[0].unreadCount).toBe(0);
  });

  it("is idempotent, so re-applying a pending update over fresh pages is safe", () => {
    const once = applySuggestionUpdate(pages, "a", { state: "read" });
    expect(applySuggestionUpdate(once, "a", { state: "read" })).toEqual(once);
  });

  it("leaves a capped count as the server sent it", () => {
    const capped = [page([item()], { unreadCount: 100, unreadCountCapped: true })];
    expect(applySuggestionUpdate(capped, "a", { state: "read" })[0].unreadCount).toBe(100);
  });
});

describe("reconcileSuggestion", () => {
  it("replaces an open item and moves the unread count", () => {
    const pages = [page([item({ id: "a" })])];
    const next = reconcileSuggestion(pages, item({ id: "a", state: "read", title: "new" }));
    expect(next[0].data[0]).toMatchObject({ state: "read", title: "new" });
    expect(next[0].unreadCount).toBe(0);
  });

  it("drops a resolved item and ignores one the cache no longer holds", () => {
    const pages = [page([item({ id: "a" })])];
    expect(reconcileSuggestion(pages, item({ id: "a", state: "dismissed" }))[0]).toMatchObject({
      data: [],
      unreadCount: 0,
    });
    expect(reconcileSuggestion(pages, item({ id: "z", state: "read" }))).toEqual(pages);
  });
});

it("maps resolutions to states like the server", () => {
  expect(stateAfterUpdate({ resolution: "none" })).toBe("read");
  expect(stateAfterUpdate({ resolution: "already_done" })).toBe("done");
  expect(stateAfterUpdate({ resolution: "action_taken", resolutionAction: 0 })).toBe("done");
  expect(stateAfterUpdate({ resolution: "useful" })).toBe("dismissed");
  expect(stateAfterUpdate({ state: "unread" })).toBe("unread");
});

it("shows a capped count with a plus", () => {
  expect(unreadCountText(100, true)).toBe("100+");
  expect(unreadCountText(7, false)).toBe("7");
});

describe("suggestionLinkTarget", () => {
  it("routes sessions inside the app", () => {
    expect(suggestionLinkTarget({ type: "session", ref: "sess 1" })).toEqual({
      kind: "session",
      path: "/c/sess%201",
    });
  });

  it("allows only web links", () => {
    expect(suggestionLinkTarget({ type: "pr", ref: "https://github.com/acme/app/pull/7" })).toEqual(
      { kind: "external", href: "https://github.com/acme/app/pull/7" },
    );
    // Like the server: an explicit scheme, a host, and no credentials.
    expect(suggestionLinkTarget({ type: "pr", ref: "github.com/acme/app/pull/7" })).toBeNull();
    expect(suggestionLinkTarget({ type: "url", ref: "https://user:pw@example.com/" })).toBeNull();
    expect(suggestionLinkTarget({ type: "url", ref: "https://user@example.com/" })).toBeNull();
    expect(suggestionLinkTarget({ type: "url", ref: "ftp://example.com/" })).toBeNull();
    expect(suggestionLinkTarget({ type: "url", ref: "javascript:alert(1)" })).toBeNull();
    expect(suggestionLinkTarget({ type: "url", ref: "data:text/html,x" })).toBeNull();
    expect(suggestionLinkTarget({ type: "url", ref: "https://[" })).toBeNull();
    expect(suggestionLinkTarget({ type: "url", ref: " " })).toBeNull();
  });
});

describe("suggestionPreview", () => {
  it("uses the first non-empty line without markdown markers", () => {
    expect(suggestionPreview("\n## Heading\nmore")).toBe("Heading");
    expect(suggestionPreview("- item")).toBe("item");
    expect(suggestionPreview("x".repeat(10), 5)).toBe("xxxx…");
    expect(suggestionPreview("")).toBe("");
  });
});
