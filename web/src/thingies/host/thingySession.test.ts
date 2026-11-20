import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";
import type * as SessionsApiModule from "@/lib/sessionsApi";

import { BridgeError } from "./thingyBridge";
import {
  adoptStartedThingySession,
  forgetThingySessionStarts,
  onSessionThingiesChanged,
  openThingyFullScreen,
  openThingyInSession,
  quoteInThingySession,
  requestThingyAction,
  thingySelectionQuote,
} from "./thingySession";

// The message posted to the session, the side panel and composer requests, and the requests to
// the thingy routes are the observables, so only those transports are replaced.
const sendUserMessage = vi.fn((_sessionId: string, _text: string) => Promise.resolve({}));
const queueThingyComposerQuote = vi.fn((_sessionId: string, _kind: string, _text: string) => {});
const requestThingyRailTab = vi.fn(
  (
    _sessionId: string,
    _key: string,
    _options?: { maximize?: boolean; keepSidebar?: boolean },
  ) => {},
);
const exitThingyRailFullScreen = vi.fn((_sessionId: string | null) => {});
const setThingyNewSessionTabMaximized = vi.fn((_maximized: boolean) => {});
// Whether the thingy fills the screen; a test sets it.
let fillsScreen = false;
const requestThingyCreateSessionMenu = vi.fn(() => {});
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  sendUserMessage: (sessionId: string, text: string) => sendUserMessage(sessionId, text),
}));
vi.mock("@/lib/thingyComposerQuotes", () => ({
  queueThingyComposerQuote: (sessionId: string, kind: string, text: string) =>
    queueThingyComposerQuote(sessionId, kind, text),
}));
vi.mock("@/lib/thingyRailTabs", () => ({
  THINGY_FULL_SCREEN_PARAM: "thingy",
  requestThingyRailTab: (
    ...args: [string, string, { maximize?: boolean; keepSidebar?: boolean }?]
  ) => requestThingyRailTab(...args),
  exitThingyRailFullScreen: (sessionId: string | null) => exitThingyRailFullScreen(sessionId),
  setThingyNewSessionTabMaximized: (maximized: boolean) =>
    setThingyNewSessionTabMaximized(maximized),
  thingyFillsScreen: () => fillsScreen,
  requestThingyCreateSessionMenu: () => requestThingyCreateSessionMenu(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: (input: RequestInfo | URL, init?: RequestInit) =>
    fakeServer.fetch(input, init),
}));

// A small stand-in for a request-mocking server: handlers answer by method and path, and every
// request is recorded so a test can wait for it and read its body.
interface MockResult {
  status: number;
  body: unknown;
}
type Transform = (result: MockResult) => void;
type Handler = (
  req: unknown,
  res: (...transforms: Transform[]) => MockResult,
  ctx: MockContext,
) => MockResult;
interface MockContext {
  json: (body: unknown) => Transform;
  status: (status: number) => Transform;
}
const mockContext: MockContext = {
  json:
    (body: unknown): Transform =>
    (result) => {
      result.body = body;
    },
  status:
    (status: number): Transform =>
    (result) => {
      result.status = status;
    },
};
function respond(...transforms: Transform[]): MockResult {
  const result: MockResult = { status: 200, body: undefined };
  transforms.forEach((transform) => transform(result));
  return result;
}
interface Route {
  method: string;
  pattern: RegExp;
  handler: Handler;
}
function route(method: string) {
  return (path: string, handler: Handler): Route => ({
    method,
    pattern: new RegExp(`^${path.replace(/:[A-Za-z]+/g, "[^/?]+")}(\\?.*)?$`),
    handler,
  });
}
const rest = { get: route("GET"), post: route("POST"), put: route("PUT"), delete: route("DELETE") };
interface RecordedRequest {
  method: string;
  path: string;
  json: () => Promise<unknown>;
}
function createFakeServer() {
  let routes: Route[] = [];
  let requests: RecordedRequest[] = [];
  let waiters: { method: string; path: string; resolve: (request: RecordedRequest) => void }[] = [];
  return {
    reset() {
      routes = [];
      requests = [];
      waiters = [];
    },
    use(...added: Route[]) {
      routes = [...added, ...routes];
    },
    waitForRequest(method: string, path: string): Promise<RecordedRequest> {
      const found = requests.find((r) => r.method === method.toUpperCase() && r.path === path);
      if (found) return Promise.resolve(found);
      return new Promise((resolve) => {
        waiters.push({ method: method.toUpperCase(), path, resolve });
      });
    },
    async fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
      const path = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      const text = typeof init?.body === "string" ? init.body : "";
      const request: RecordedRequest = {
        method,
        path,
        json: async () => (text ? JSON.parse(text) : undefined),
      };
      requests.push(request);
      waiters = waiters.filter((waiter) => {
        if (waiter.method === method && waiter.path === path) {
          waiter.resolve(request);
          return false;
        }
        return true;
      });
      const match = routes.find((r) => r.method === method && r.pattern.test(path));
      const result = match
        ? match.handler(request, respond, mockContext)
        : { status: 404, body: {} };
      return new Response(result.body === undefined ? null : JSON.stringify(result.body), {
        status: result.status,
        headers: { "Content-Type": "application/json" },
      });
    },
  };
}
const fakeServer = createFakeServer();

function parse(path: string): URL {
  return new URL(path, "https://omnigent.example.com");
}

const API = "/v1/thingies";
const EDITOR = {
  id: "app-1",
  name: "Team board",
  permissions: { read: true, write_data: true, publish: true, manage: false },
};
const ENTRY = {
  id: "act1",
  version: "v1",
  value: { action: "run_card", params: { cardId: "c1" }, state: "queued" },
};

// message posted to the session and the page opened beside the thingy are the observables.
describe("thingy session", () => {
  const server = fakeServer;
  const waitForRequest = (method: string, path: string) => fakeServer.waitForRequest(method, path);
  const navigate = vi.fn((_url: string) => undefined);

  beforeEach(() => {
    fakeServer.reset();
    sendUserMessage.mockClear();
    queueThingyComposerQuote.mockClear();
    requestThingyRailTab.mockClear();
    exitThingyRailFullScreen.mockClear();
    setThingyNewSessionTabMaximized.mockClear();
    requestThingyCreateSessionMenu.mockClear();
    fillsScreen = false;
    forgetThingySessionStarts();
    navigate.mockClear();
  });

  it("opens the thingy in full screen in the viewer's thingy session", async () => {
    server.use(
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_1", version: "v" })),
      ),
      rest.put(`${API}/app-1/sessions/s_1`, (_req, res, ctx) => res(ctx.json({ items: [] }))),
    );
    const linked = waitForRequest("put", `${API}/app-1/sessions/s_1`);

    await openThingyFullScreen(EDITOR, navigate);

    await linked;
    expect(requestThingyRailTab).toHaveBeenCalledWith("s_1", "thingy:app-1", {
      maximize: true,
      keepSidebar: false,
    });
    expect(parse(navigate.mock.calls[0][0]).pathname).toBe("/c/s_1");
    expect(setThingyNewSessionTabMaximized).not.toHaveBeenCalled();
  });

  it("opens the thingy in full screen beside New session when the thingy session is gone", async () => {
    server.use(
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_old", version: "v" })),
      ),
      rest.put(`${API}/app-1/sessions/s_old`, (_req, res, ctx) =>
        res(ctx.status(404), ctx.json({})),
      ),
    );

    await openThingyFullScreen(EDITOR, navigate);

    expect(requestThingyRailTab).not.toHaveBeenCalled();
    expect(setThingyNewSessionTabMaximized).toHaveBeenCalledWith(true);
    const opened = parse(navigate.mock.calls[0][0]);
    expect(opened.pathname).toBe("/");
    expect(opened.searchParams.get("thingy_start")).toBe("app-1");
    // The link itself names the thingy filling the screen, so a link with a prompt alone means
    // the composer should show.
    expect(opened.searchParams.get("thingy")).toBe("app-1");
    expect(opened.searchParams.get("prompt")).toContain(
      'This is my thingy session for "Team board"',
    );
  });

  it("opens New session without instructions for a viewer who cannot edit the thingy", async () => {
    const viewer = {
      ...EDITOR,
      permissions: { read: true, write_data: false, publish: false, manage: false },
    };

    await openThingyFullScreen(viewer, navigate);

    const opened = parse(navigate.mock.calls[0][0]);
    expect(opened.searchParams.get("thingy_start")).toBe("app-1");
    expect(opened.searchParams.has("prompt")).toBe(false);
    expect(setThingyNewSessionTabMaximized).toHaveBeenCalledWith(true);
  });

  it("leaves full screen when a quote goes to the composer of the session beside it", async () => {
    await quoteInThingySession(EDITOR, "reply", "Card A", navigate, "s_rail");

    expect(queueThingyComposerQuote).toHaveBeenCalledWith("s_rail", "reply", "Card A");
    expect(exitThingyRailFullScreen).toHaveBeenCalledWith("s_rail");
  });

  it("queues the action and posts it to the viewer's thingy session, without opening the pane", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_1", version: "v" })),
      ),
      rest.post(`${API}/app-1/actions/act1/delivery`, (_req, res, ctx) =>
        res(ctx.json({ items: [] })),
      ),
    );
    const queued = waitForRequest("post", `${API}/app-1/actions`);
    const delivered = waitForRequest("post", `${API}/app-1/actions/act1/delivery`);

    const receipt = await requestThingyAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" } },
      navigate,
    );

    expect(await (await queued).json()).toMatchObject({
      action: "run_card",
      params: { cardId: "c1" },
      operation_id: expect.any(String),
    });
    expect(receipt).toEqual({ actionId: "act1", delivery: "sent" });
    expect(sendUserMessage).toHaveBeenCalledWith(
      "s_1",
      'Thingy action\n{"action_id":"act1","action":"run_card","params":{"cardId":"c1"}}',
    );
    // The session works without the viewer leaving the thingy.
    expect(navigate).not.toHaveBeenCalled();
    // Recording the delivery lets Omnigent follow the action through the sessions doing it.
    expect(await (await delivered).json()).toEqual({ session_id: "s_1" });
  });

  it("joins a request that is still open in a session instead of sending it again", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) =>
        res(ctx.json({ ...ENTRY, merged: true, delivered: true })),
      ),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_1", version: "v" })),
      ),
    );

    const receipt = await requestThingyAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" } },
      navigate,
    );

    expect(receipt).toEqual({ actionId: "act1", delivery: "merged" });
    expect(sendUserMessage).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("sends a merged request again when the open one never reached a session", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) =>
        res(ctx.json({ ...ENTRY, merged: true })),
      ),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: null, version: null })),
      ),
    );

    const receipt = await requestThingyAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" } },
      navigate,
    );

    expect(receipt).toEqual({ actionId: "act1", delivery: "starting" });
    // New session opens again with the action, so starting it delivers the open entry. The link
    // leaves a full screen thingy docked so the composer shows.
    const link = parse(navigate.mock.calls[0][0]);
    expect(link.searchParams.has("thingy")).toBe(false);
    const prompt = link.searchParams.get("prompt") ?? "";
    expect(
      prompt.endsWith(
        'Thingy action\n{"action_id":"act1","action":"run_card","params":{"cardId":"c1"}}',
      ),
    ).toBe(true);
  });

  it("starts the thingy session when the viewer has none, and shows the thingy beside it once started", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: null, version: null })),
      ),
      rest.put(`${API}/app-1/sessions/:sessionId`, (_req, res, ctx) =>
        res(ctx.json({ items: [] })),
      ),
      rest.post(`${API}/app-1/actions/act1/delivery`, (_req, res, ctx) =>
        res(ctx.json({ items: [] })),
      ),
    );

    const receipt = await requestThingyAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" } },
      navigate,
    );

    expect(receipt).toEqual({ actionId: "act1", delivery: "starting" });
    expect(sendUserMessage).not.toHaveBeenCalled();
    const opened = parse(navigate.mock.calls[0][0]);
    expect(opened.pathname).toBe("/");
    expect(opened.searchParams.get("thingy_start")).toBe("app-1");
    const prompt = opened.searchParams.get("prompt") ?? "";
    expect(prompt.startsWith("Load the thingy skill and follow it.")).toBe(true);
    expect(prompt).toContain('This is my thingy session for "Team board" (thingy_id app-1):');
    // A short note: the session reads how to coordinate from the thingy skill.
    expect(prompt).toContain("sys_thingy_* tools");
    expect(prompt.split("\n\n")[0].length).toBeLessThan(400);
    expect(
      prompt.endsWith(
        'Thingy action\n{"action_id":"act1","action":"run_card","params":{"cardId":"c1"}}',
      ),
    ).toBe(true);

    expect(await adoptStartedThingySession("app-1", "temp:123")).toBe(false);
    const linked = waitForRequest("put", `${API}/app-1/sessions/s_new`);
    const delivered = waitForRequest("post", `${API}/app-1/actions/act1/delivery`);
    expect(await adoptStartedThingySession("app-1", "s_new")).toBe(true);
    // Shown beside the started session, which also makes it the viewer's thingy session.
    expect(await (await linked).json()).toEqual({ linked: true });
    // The action rode in the started session's first message, so it was delivered there.
    expect(await (await delivered).json()).toEqual({ session_id: "s_new" });
  });

  it("keeps a full screen thingy full screen and opens its Create session menu", async () => {
    fillsScreen = true;
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: null, version: null })),
      ),
    );

    const receipt = await requestThingyAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" } },
      navigate,
    );

    expect(receipt).toEqual({ actionId: "act1", delivery: "starting" });
    const opened = parse(navigate.mock.calls[0][0]);
    expect(opened.searchParams.get("thingy_start")).toBe("app-1");
    // The URL keeps the thingy full screen beside New session.
    expect(opened.searchParams.get("thingy")).toBe("app-1");
    expect(opened.searchParams.get("prompt")).toContain('"action_id":"act1"');
    expect(requestThingyCreateSessionMenu).toHaveBeenCalledTimes(1);
    expect(exitThingyRailFullScreen).not.toHaveBeenCalled();
  });

  it("starts one session for several requests made before it exists", async () => {
    const second = { ...ENTRY, id: "act2", value: { ...ENTRY.value, params: { cardId: "c2" } } };
    let next = ENTRY;
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(next))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: null, version: null })),
      ),
      rest.put(`${API}/app-1/sessions/:sessionId`, (_req, res, ctx) =>
        res(ctx.json({ items: [] })),
      ),
      rest.post(`${API}/app-1/actions/:actionId/delivery`, (_req, res, ctx) =>
        res(ctx.json({ items: [] })),
      ),
    );

    await requestThingyAction(EDITOR, { action: "run_card", params: { cardId: "c1" } }, navigate);
    next = second;
    await requestThingyAction(EDITOR, { action: "run_card", params: { cardId: "c2" } }, navigate);

    // The second link carries both requests, so the one session started handles both.
    const prompt = parse(navigate.mock.calls[1][0]).searchParams.get("prompt") ?? "";
    expect(prompt).toContain('"action_id":"act1"');
    expect(prompt).toContain('"action_id":"act2"');
    expect(prompt.match(/Load the thingy skill/g)).toHaveLength(1);
    const first = waitForRequest("post", `${API}/app-1/actions/act1/delivery`);
    const other = waitForRequest("post", `${API}/app-1/actions/act2/delivery`);
    expect(await adoptStartedThingySession("app-1", "s_new")).toBe(true);
    expect(await (await first).json()).toEqual({ session_id: "s_new" });
    expect(await (await other).json()).toEqual({ session_id: "s_new" });
  });

  it("sends actions and quotes to the session the thingy is shown beside", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.post(`${API}/app-1/actions/act1/delivery`, (_req, res, ctx) =>
        res(ctx.json({ items: [] })),
      ),
    );
    const delivered = waitForRequest("post", `${API}/app-1/actions/act1/delivery`);

    const receipt = await requestThingyAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" } },
      navigate,
      "s_rail",
    );
    await quoteInThingySession(EDITOR, "reply", "Fix login", navigate, "s_rail");

    expect(receipt).toEqual({ actionId: "act1", delivery: "sent" });
    expect(sendUserMessage).toHaveBeenCalledWith(
      "s_rail",
      expect.stringContaining("Thingy action"),
    );
    expect(await (await delivered).json()).toEqual({ session_id: "s_rail" });
    expect(queueThingyComposerQuote).toHaveBeenCalledWith("s_rail", "reply", "Fix login");
    // The session is already on screen beside the thingy.
    expect(navigate).not.toHaveBeenCalled();
  });

  it("starts a new thingy session when the remembered one is gone", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_gone", version: "v" })),
      ),
    );
    sendUserMessage.mockRejectedValueOnce(
      Object.assign(new Error("Session not found"), { status: 404 }),
    );

    const receipt = await requestThingyAction(EDITOR, { action: "run_card", params: {} }, navigate);

    expect(receipt).toMatchObject({ delivery: "starting" });
    expect(parse(navigate.mock.calls[0][0]).pathname).toBe("/");
  });

  it("reports a thingy session that could not take the request, keeping the viewer's place", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_1", version: "v" })),
      ),
    );
    sendUserMessage.mockRejectedValueOnce(
      Object.assign(new Error("Host offline"), { status: 503 }),
    );

    await expect(
      requestThingyAction(EDITOR, { action: "run_card", params: {} }, navigate),
    ).rejects.toMatchObject({ code: "UNAVAILABLE" });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("quotes a selection in the thingy session composer, like Reply in a session", async () => {
    server.use(
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_1", version: "v" })),
      ),
    );
    const quote = thingySelectionQuote(EDITOR, {
      text: "Fix login",
      rect: { x: 0, y: 0, width: 0, height: 0 },
      occurrence: 2,
      ref: "cards/c1#title",
      label: "Card",
    });

    await quoteInThingySession(EDITOR, "side_chat", quote, navigate);

    expect(quote).toBe(
      'Fix login\n— Card in "Team board" (thingy_id app-1, ref cards/c1#title, occurrence 2)',
    );
    expect(queueThingyComposerQuote).toHaveBeenCalledWith("s_1", "side_chat", quote);
    // The thingy session opens with the thingy in its side panel.
    expect(requestThingyRailTab).toHaveBeenCalledWith("s_1", "thingy:app-1");
    expect(parse(navigate.mock.calls[0][0]).pathname).toBe("/c/s_1");
  });

  it("starts the thingy session with the quote when the viewer has none", async () => {
    server.use(
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: null, version: null })),
      ),
    );

    await quoteInThingySession(
      EDITOR,
      "reply",
      'Fix login\n— "Team board" (thingy_id app-1)',
      navigate,
    );

    expect(queueThingyComposerQuote).not.toHaveBeenCalled();
    const opened = parse(navigate.mock.calls[0][0]);
    expect(opened.pathname).toBe("/");
    expect(opened.searchParams.get("prompt")).toContain(
      '> Fix login\n> — "Team board" (thingy_id app-1)',
    );
  });

  it("links the thingy to the session it opens, so its side panel shows the thingy", async () => {
    const linkedApps = [
      {
        ...EDITOR,
        owner_id: "owner@example.com",
        viewer_id: "viewer@example.com",
        published_revision: null,
        version: null,
      },
    ];
    server.use(
      rest.put(`${API}/app-1/sessions/:sessionId`, (_req, res, ctx) =>
        res(ctx.json({ items: linkedApps })),
      ),
    );
    const linked = waitForRequest("put", `${API}/app-1/sessions/s_1`);
    const changed = vi.fn();
    const unsubscribe = onSessionThingiesChanged(changed);

    await openThingyInSession("app-1", "s_1", navigate);

    expect(await (await linked).json()).toEqual({ linked: true });
    expect(changed).toHaveBeenCalledWith("s_1", [expect.objectContaining({ id: "app-1" })]);
    expect(requestThingyRailTab).toHaveBeenCalledWith("s_1", "thingy:app-1");
    expect(navigate).toHaveBeenCalledWith("/c/s_1");
    unsubscribe();
  });

  it("still opens the session when the thingy cannot be linked there", async () => {
    server.use(
      rest.put(`${API}/app-1/sessions/:sessionId`, (_req, res, ctx) => res(ctx.status(403))),
    );

    await openThingyInSession("app-1", "s_1", navigate);

    expect(navigate).toHaveBeenCalledWith("/c/s_1");
  });

  it("refuses a viewer who cannot edit the thingy", async () => {
    const actions = vi.fn();
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => {
        actions();
        return res(ctx.status(201), ctx.json(ENTRY));
      }),
    );

    await expect(
      requestThingyAction(
        { ...EDITOR, permissions: { ...EDITOR.permissions, publish: false } },
        { action: "run_card", params: {} },
        navigate,
      ),
    ).rejects.toEqual(
      new BridgeError(
        "PERMISSION_DENIED",
        "Running actions on this thingy needs Can edit thingy access.",
      ),
    );
    expect(actions).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });
});
