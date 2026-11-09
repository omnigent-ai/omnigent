import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import {
  applySuggestionUpdate,
  listSuggestions,
  suggestionLinkTarget,
  suggestionPreview,
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
  suggested_prompt: null,
  source_session_id: "sess",
  state: "unread",
  feedback: "none",
  created_at: 10,
};

function item(overrides: Partial<Suggestion> = {}): Suggestion {
  return {
    id: "a",
    kind: "info",
    title: "t",
    bodyMd: "b",
    links: [],
    suggestedPrompt: null,
    sourceSessionId: "s",
    state: "unread",
    feedback: "none",
    createdAt: 1,
    ...overrides,
  };
}

describe("API calls", () => {
  beforeEach(() => mockFetch.mockReset());

  it("lists open suggestions with the unread count", async () => {
    mockFetch.mockResolvedValue(response({ data: [wire], next_cursor: null, unread_count: 3 }));

    const page = await listSuggestions();

    expect(mockFetch.mock.calls[0][0]).toBe("/v1/inbox/items?limit=50&state=unread&state=read");
    expect(page.unreadCount).toBe(3);
    expect(page.data[0]).toMatchObject({ id: "sug_1", bodyMd: "Body", sourceSessionId: "sess" });
  });

  it("patches one suggestion", async () => {
    mockFetch.mockResolvedValue(response({ ...wire, state: "dismissed" }));

    const updated = await updateSuggestion("sug_1", { state: "dismissed" });

    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe("/v1/inbox/items/sug_1");
    expect(init).toMatchObject({ method: "PATCH", body: JSON.stringify({ state: "dismissed" }) });
    expect(updated.state).toBe("dismissed");
  });
});

describe("applySuggestionUpdate", () => {
  const page: SuggestionPage = {
    data: [item({ id: "a" }), item({ id: "b", state: "read" })],
    unreadCount: 1,
  };

  it("counts a read and keeps the item listed", () => {
    const next = applySuggestionUpdate(page, "a", { state: "read" });
    expect(next.unreadCount).toBe(0);
    expect(next.data.map((entry) => entry.state)).toEqual(["read", "read"]);
  });

  it("drops dismissed and done items", () => {
    expect(applySuggestionUpdate(page, "b", { state: "done" }).data.map((e) => e.id)).toEqual([
      "a",
    ]);
    expect(applySuggestionUpdate(page, "a", { state: "dismissed" })).toEqual({
      data: [page.data[1]],
      unreadCount: 0,
    });
  });

  it("keeps feedback-only updates listed", () => {
    const next = applySuggestionUpdate(page, "b", { feedback: "useful" });
    expect(next.data[1].feedback).toBe("useful");
    expect(next.unreadCount).toBe(1);
  });
});

describe("suggestionLinkTarget", () => {
  it("routes sessions inside the app", () => {
    expect(suggestionLinkTarget({ type: "session", ref: "sess 1" })).toEqual({
      kind: "session",
      path: "/c/sess%201",
    });
  });

  it("allows only web links", () => {
    expect(suggestionLinkTarget({ type: "pr", ref: "github.com/acme/app/pull/7" })).toEqual({
      kind: "external",
      href: "https://github.com/acme/app/pull/7",
    });
    expect(suggestionLinkTarget({ type: "url", ref: "javascript:alert(1)" })).toBeNull();
    expect(suggestionLinkTarget({ type: "url", ref: "data:text/html,x" })).toBeNull();
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
