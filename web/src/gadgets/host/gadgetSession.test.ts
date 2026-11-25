import { waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";
import type * as SessionsApiModule from "@/lib/sessionsApi";

import { BridgeError } from "./gadgetBridge";
import {
  acknowledgeStartedGadgetInput,
  recordGadgetDeliveries,
  adoptStartedGadgetSession,
  forgetGadgetSessionStarts,
  gadgetSessionStarting,
  setGadgetSessionStarting,
  onSessionGadgetsChanged,
  openGadgetFullScreen,
  openGadgetInSession,
  openGadgetSession,
  quoteInGadgetSession,
  requestGadgetAction,
  gadgetSelectionQuote,
  mayHaveBeenDelivered,
} from "./gadgetSession";

// The message posted to the session, the side panel and composer requests, and the requests to
// the gadget routes are the observables, so only those transports are replaced.
const sendUserMessage = vi.fn((_sessionId: string, _text: string) => Promise.resolve({}));
const queueGadgetComposerQuote = vi.fn((_sessionId: string, _kind: string, _text: string) => {});
const requestGadgetRailTab = vi.fn(
  (
    _sessionId: string,
    _key: string,
    _options?: { maximize?: boolean; keepSidebar?: boolean },
  ) => {},
);
const exitGadgetRailFullScreen = vi.fn((_sessionId: string | null) => {});
const setGadgetNewSessionTabMaximized = vi.fn((_maximized: boolean) => {});
// Whether the gadget fills the screen; a test sets it.
let fillsScreen = false;
const requestGadgetCreateSessionMenu = vi.fn(() => {});
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  sendUserMessage: (sessionId: string, text: string) => sendUserMessage(sessionId, text),
}));
vi.mock("@/lib/gadgetComposerQuotes", () => ({
  queueGadgetComposerQuote: (sessionId: string, kind: string, text: string) =>
    queueGadgetComposerQuote(sessionId, kind, text),
}));
vi.mock("@/lib/gadgetRailTabs", () => ({
  GADGET_FULL_SCREEN_PARAM: "gadget",
  requestGadgetRailTab: (
    ...args: [string, string, { maximize?: boolean; keepSidebar?: boolean }?]
  ) => requestGadgetRailTab(...args),
  exitGadgetRailFullScreen: (sessionId: string | null) => exitGadgetRailFullScreen(sessionId),
  setGadgetNewSessionTabMaximized: (maximized: boolean) =>
    setGadgetNewSessionTabMaximized(maximized),
  gadgetFillsScreen: () => fillsScreen,
  requestGadgetCreateSessionMenu: () => requestGadgetCreateSessionMenu(),
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
) => MockResult | Promise<MockResult>;
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
    requests(method: string, path: string): RecordedRequest[] {
      return requests.filter((r) => r.method === method.toUpperCase() && r.path === path);
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
        ? await match.handler(request, respond, mockContext)
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

const API = "/v1/gadgets";
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

// message posted to the session and the page opened beside the gadget are the observables.
describe("gadget session", () => {
  const server = fakeServer;
  const waitForRequest = (method: string, path: string) => fakeServer.waitForRequest(method, path);
  const navigate = vi.fn((_url: string) => undefined);

  beforeEach(() => {
    fakeServer.reset();
    sendUserMessage.mockClear();
    queueGadgetComposerQuote.mockClear();
    requestGadgetRailTab.mockClear();
    exitGadgetRailFullScreen.mockClear();
    setGadgetNewSessionTabMaximized.mockClear();
    requestGadgetCreateSessionMenu.mockClear();
    fillsScreen = false;
    forgetGadgetSessionStarts();
    navigate.mockClear();
  });

  it("asks again for nothing while the viewer's gadget session is being created", async () => {
    const queued = vi.fn();
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => {
        queued();
        return res(ctx.json({ id: "act1", delivered: false, merged: false }));
      }),
    );
    setGadgetSessionStarting("app-1", true);
    expect(gadgetSessionStarting("app-1")).toBe(true);

    // The session being created carries the earlier requests; a new one now would ask for
    // another session.
    await expect(
      requestGadgetAction(EDITOR, { action: "run_card", params: { cardId: "c1" } }, navigate),
    ).rejects.toMatchObject({ code: "UNAVAILABLE" });
    expect(queued).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
    expect(requestGadgetCreateSessionMenu).not.toHaveBeenCalled();

    setGadgetSessionStarting("app-1", false);
    expect(gadgetSessionStarting("app-1")).toBe(false);
  });

  it("opens the gadget in full screen in the viewer's gadget session", async () => {
    server.use(
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_1", version: "v" })),
      ),
      rest.put(`${API}/app-1/sessions/s_1`, (_req, res, ctx) => res(ctx.json({ items: [] }))),
    );
    const linked = waitForRequest("put", `${API}/app-1/sessions/s_1`);

    await openGadgetFullScreen(EDITOR, navigate);

    await linked;
    expect(requestGadgetRailTab).toHaveBeenCalledWith("s_1", "gadget:app-1", {
      maximize: true,
      keepSidebar: false,
    });
    expect(parse(navigate.mock.calls[0][0]).pathname).toBe("/c/s_1");
    expect(setGadgetNewSessionTabMaximized).not.toHaveBeenCalled();
  });

  it("opens the gadget in full screen beside New session when the gadget session is gone", async () => {
    server.use(
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_old", version: "v" })),
      ),
      rest.put(`${API}/app-1/sessions/s_old`, (_req, res, ctx) =>
        res(ctx.status(404), ctx.json({})),
      ),
    );

    await openGadgetFullScreen(EDITOR, navigate);

    expect(requestGadgetRailTab).not.toHaveBeenCalled();
    expect(setGadgetNewSessionTabMaximized).toHaveBeenCalledWith(true);
    const opened = parse(navigate.mock.calls[0][0]);
    expect(opened.pathname).toBe("/");
    expect(opened.searchParams.get("gadget_start")).toBe("app-1");
    // The link itself names the gadget filling the screen, so a link with a prompt alone means
    // the composer should show.
    expect(opened.searchParams.get("gadget")).toBe("app-1");
    expect(opened.searchParams.get("prompt")).toContain(
      'This is my gadget session for "Team board"',
    );
  });

  it("opens New session without instructions for a viewer who cannot edit the gadget", async () => {
    const viewer = {
      ...EDITOR,
      permissions: { read: true, write_data: false, publish: false, manage: false },
    };

    await openGadgetFullScreen(viewer, navigate);

    const opened = parse(navigate.mock.calls[0][0]);
    expect(opened.searchParams.get("gadget_start")).toBe("app-1");
    expect(opened.searchParams.has("prompt")).toBe(false);
    expect(setGadgetNewSessionTabMaximized).toHaveBeenCalledWith(true);
  });

  it("leaves full screen when a quote goes to the composer of the session beside it", async () => {
    await quoteInGadgetSession(EDITOR, "reply", "Card A", navigate, "s_rail");

    expect(queueGadgetComposerQuote).toHaveBeenCalledWith("s_rail", "reply", "Card A");
    expect(exitGadgetRailFullScreen).toHaveBeenCalledWith("s_rail");
  });

  it("queues the action and posts it to the viewer's gadget session, without opening the pane", async () => {
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

    const receipt = await requestGadgetAction(
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
      'Gadget action\n{"action_id":"act1","action":"run_card","params":{"cardId":"c1"}}',
    );
    // The session works without the viewer leaving the gadget.
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

    const receipt = await requestGadgetAction(
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

    const receipt = await requestGadgetAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" } },
      navigate,
    );

    expect(receipt).toEqual({ actionId: "act1", delivery: "starting" });
    // New session opens again with the action, so starting it delivers the open entry. The link
    // leaves a full screen gadget docked so the composer shows.
    const link = parse(navigate.mock.calls[0][0]);
    expect(link.searchParams.has("gadget")).toBe(false);
    const prompt = link.searchParams.get("prompt") ?? "";
    expect(
      prompt.endsWith(
        'Gadget action\n{"action_id":"act1","action":"run_card","params":{"cardId":"c1"}}',
      ),
    ).toBe(true);
  });

  it("refuses an action a policy in the gadget session denies, and records no delivery", async () => {
    let deliveries = 0;
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_1", version: "v" })),
      ),
      rest.post(`${API}/app-1/actions/act1/delivery`, (_req, res, ctx) => {
        deliveries += 1;
        return res(ctx.json({ items: [] }));
      }),
    );
    // The events route answers a refused message like an accepted one, with `denied` set.
    sendUserMessage.mockResolvedValueOnce({ queued: false, denied: true });

    const request = requestGadgetAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" } },
      navigate,
    );

    await expect(request).rejects.toMatchObject({ code: "PERMISSION_DENIED" });
    expect(deliveries).toBe(0);
  });

  it("reports Address all's message refused by a policy, so its comments stay open", async () => {
    server.use(
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_1", version: "v" })),
      ),
    );
    sendUserMessage.mockResolvedValueOnce({ queued: false, denied: true });
    const onDelivered = vi.fn();

    await expect(
      openGadgetSession(EDITOR, "Address these comments", navigate, {
        deliver: true,
        reveal: false,
        onDelivered,
      }),
    ).rejects.toMatchObject({ code: "PERMISSION_DENIED" });
    expect(onDelivered).not.toHaveBeenCalled();
  });

  it.each([
    ["a 4xx answer", Object.assign(new Error("Bad request"), { status: 400 }), false],
    ["no answer", new TypeError("Failed to fetch"), true],
    ["a 5xx answer", Object.assign(new Error("Bad gateway"), { status: 502 }), true],
  ])(
    "says whether a message that failed with %s may still have reached the session",
    async (_label, failure, maybe) => {
      server.use(
        rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
          res(ctx.json({ session_id: "s_1", version: "v" })),
        ),
      );
      sendUserMessage.mockRejectedValueOnce(failure);

      const error = await openGadgetSession(EDITOR, "Build it", navigate, {
        deliver: true,
        reveal: false,
      }).catch((caught: unknown) => caught);

      expect(mayHaveBeenDelivered(error)).toBe(maybe);
    },
  );

  it("asks with the caller's key, and sends nothing for a request cancelled before its message left", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
    );
    const queued = waitForRequest("post", `${API}/app-1/actions`);
    const controller = new AbortController();
    controller.abort();

    const request = requestGadgetAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" }, operationId: "req-1" },
      navigate,
      undefined,
      controller.signal,
    );

    await expect(request).rejects.toMatchObject({ code: "CANCELLED" });
    expect(await (await queued).json()).toMatchObject({ operation_id: "req-1" });
    expect(sendUserMessage).not.toHaveBeenCalled();
  });

  it.each([
    ["an existing gadget session", "s_1"],
    ["no gadget session", null],
  ])(
    "sends, queues and opens nothing when cancelled while it looks up %s",
    async (_label, sessionId) => {
      let answer: () => void = () => {};
      const answered = new Promise<void>((resolve) => {
        answer = resolve;
      });
      server.use(
        rest.post(`${API}/app-1/actions`, (_req, res, ctx) =>
          res(ctx.status(201), ctx.json(ENTRY)),
        ),
        rest.get(`${API}/app-1/session`, async (_req, res, ctx) => {
          await answered;
          return res(ctx.json({ session_id: sessionId, version: "v" }));
        }),
      );
      const lookup = waitForRequest("get", `${API}/app-1/session`);
      const controller = new AbortController();

      const request = requestGadgetAction(
        EDITOR,
        { action: "run_card", params: { cardId: "c1" }, operationId: "req-1" },
        navigate,
        undefined,
        controller.signal,
      );
      await lookup;
      controller.abort();
      answer();

      await expect(request).rejects.toMatchObject({ code: "CANCELLED" });
      expect(sendUserMessage).not.toHaveBeenCalled();
      expect(navigate).not.toHaveBeenCalled();
      expect(fakeServer.requests("post", `${API}/app-1/actions/act1/delivery`)).toHaveLength(0);
    },
  );

  it("still takes a message the session answers without queueing it", async () => {
    server.use(
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_1", version: "v" })),
      ),
    );
    sendUserMessage.mockResolvedValueOnce({ queued: false });

    expect(
      await openGadgetSession(EDITOR, "Address these comments", navigate, {
        deliver: true,
        reveal: false,
      }),
    ).toBe("sent");
  });

  it("records a delivery it couldn't record later, without sending the request again", async () => {
    let recordFails = true;
    let deliveries = 0;
    let merged = false;
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) =>
        merged
          ? res(ctx.json({ ...ENTRY, merged: true, delivered: false }))
          : res(ctx.status(201), ctx.json(ENTRY)),
      ),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_1", version: "v" })),
      ),
      rest.post(`${API}/app-1/actions/act1/delivery`, (_req, res, ctx) => {
        deliveries += 1;
        return recordFails
          ? res(ctx.status(503), ctx.json({ error: { message: "busy" } }))
          : res(ctx.json({ items: [] }));
      }),
    );
    const request = { action: "run_card", params: { cardId: "c1" } };

    // The session gets the message; every try to record that fails.
    expect(await requestGadgetAction(EDITOR, request, navigate)).toEqual({
      actionId: "act1",
      delivery: "sent",
    });
    expect(deliveries).toBe(3);

    // The same request again joins the open entry, which the server still shows undelivered.
    merged = true;
    recordFails = false;
    expect(await requestGadgetAction(EDITOR, request, navigate)).toEqual({
      actionId: "act1",
      delivery: "merged",
    });
    expect(sendUserMessage).toHaveBeenCalledTimes(1);
    expect(deliveries).toBe(4);
    expect(navigate).not.toHaveBeenCalled();
  }, 10_000);

  it("starts the gadget session when the viewer has none, and shows the gadget beside it once started", async () => {
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

    const receipt = await requestGadgetAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" } },
      navigate,
    );

    expect(receipt).toEqual({ actionId: "act1", delivery: "starting" });
    expect(sendUserMessage).not.toHaveBeenCalled();
    const opened = parse(navigate.mock.calls[0][0]);
    expect(opened.pathname).toBe("/");
    expect(opened.searchParams.get("gadget_start")).toBe("app-1");
    const prompt = opened.searchParams.get("prompt") ?? "";
    expect(prompt.startsWith("Load the gadget skill and follow it.")).toBe(true);
    expect(prompt).toContain('This is my gadget session for "Team board" (gadget_id app-1):');
    // A short note: the session reads how to coordinate from the gadget skill.
    expect(prompt).toContain("sys_gadget_* tools");
    expect(prompt.split("\n\n")[0].length).toBeLessThan(400);
    expect(
      prompt.endsWith(
        'Gadget action\n{"action_id":"act1","action":"run_card","params":{"cardId":"c1"}}',
      ),
    ).toBe(true);

    expect(await adoptStartedGadgetSession("app-1", "temp:123")).toBe(false);
    const linked = waitForRequest("put", `${API}/app-1/sessions/s_new`);
    const delivered = waitForRequest("post", `${API}/app-1/actions/act1/delivery`);
    expect(await adoptStartedGadgetSession("app-1", "s_new")).toBe(true);
    await acknowledgeStartedGadgetInput("app-1", "s_new", prompt);
    // Shown beside the started session, which also makes it the viewer's gadget session.
    expect(await (await linked).json()).toEqual({ linked: true });
    // The action rode in the started session's first message, so it was delivered there.
    expect(await (await delivered).json()).toEqual({ session_id: "s_new" });
  });

  // Gadget full screen contract: an action from a gadget filling the screen keeps it filling the
  // screen. These pin intended behavior; change them only on purpose.
  describe("gadget full screen contract", () => {
    beforeEach(() => {
      fillsScreen = true;
    });

    it.each([
      ["the viewer's gadget session", undefined],
      ["the session the gadget is shown beside", "s_rail"],
    ])(
      "sends an action to %s without leaving full screen or opening anything",
      async (_case, railSessionId) => {
        // A session the gadget is shown beside and linked to is the viewer's gadget session.
        const sessionId = railSessionId ?? "s_1";
        server.use(
          rest.post(`${API}/app-1/actions`, (_req, res, ctx) =>
            res(ctx.status(201), ctx.json(ENTRY)),
          ),
          rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
            res(ctx.json({ session_id: sessionId, version: "v" })),
          ),
          rest.post(`${API}/app-1/actions/act1/delivery`, (_req, res, ctx) =>
            res(ctx.json({ items: [] })),
          ),
        );
        const delivered = waitForRequest("post", `${API}/app-1/actions/act1/delivery`);

        const receipt = await requestGadgetAction(
          EDITOR,
          { action: "run_card", params: { cardId: "c1" } },
          navigate,
          railSessionId,
        );

        expect(receipt).toEqual({ actionId: "act1", delivery: "sent" });
        expect(sendUserMessage).toHaveBeenCalledWith(
          sessionId,
          expect.stringContaining("Gadget action"),
        );
        expect(await (await delivered).json()).toEqual({ session_id: sessionId });
        expect(navigate).not.toHaveBeenCalled();
        expect(exitGadgetRailFullScreen).not.toHaveBeenCalled();
        expect(requestGadgetCreateSessionMenu).not.toHaveBeenCalled();
      },
    );

    it("with no session yet, keeps it full screen, asks for the Create session menu, and delivers the action in the session created", async () => {
      server.use(
        rest.post(`${API}/app-1/actions`, (_req, res, ctx) =>
          res(ctx.status(201), ctx.json(ENTRY)),
        ),
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

      await requestGadgetAction(EDITOR, { action: "run_card", params: { cardId: "c1" } }, navigate);

      // New session, with the gadget still full screen and the action as its first message.
      const opened = parse(navigate.mock.calls[0][0]);
      expect(opened.searchParams.get("gadget")).toBe("app-1");
      expect(opened.searchParams.get("gadget_start")).toBe("app-1");
      expect(opened.searchParams.get("prompt")).toContain('"action_id":"act1"');
      expect(sendUserMessage).not.toHaveBeenCalled();
      expect(requestGadgetCreateSessionMenu).toHaveBeenCalledTimes(1);
      expect(exitGadgetRailFullScreen).not.toHaveBeenCalled();

      // Create session: the session that starts is linked to the gadget and gets the action.
      const linked = waitForRequest("put", `${API}/app-1/sessions/s_new`);
      const delivered = waitForRequest("post", `${API}/app-1/actions/act1/delivery`);
      const firstMessage = opened.searchParams.get("prompt");
      expect(await adoptStartedGadgetSession("app-1", "s_new")).toBe(true);
      await acknowledgeStartedGadgetInput("app-1", "s_new", firstMessage);
      expect(await (await linked).json()).toEqual({ linked: true });
      expect(await (await delivered).json()).toEqual({ session_id: "s_new" });
      expect(exitGadgetRailFullScreen).not.toHaveBeenCalled();
    });
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

    await requestGadgetAction(EDITOR, { action: "run_card", params: { cardId: "c1" } }, navigate);
    next = second;
    await requestGadgetAction(EDITOR, { action: "run_card", params: { cardId: "c2" } }, navigate);

    // The second link carries both requests, so the one session started handles both.
    const prompt = parse(navigate.mock.calls[1][0]).searchParams.get("prompt") ?? "";
    expect(prompt).toContain('"action_id":"act1"');
    expect(prompt).toContain('"action_id":"act2"');
    expect(prompt.match(/Load the gadget skill/g)).toHaveLength(1);
    const first = waitForRequest("post", `${API}/app-1/actions/act1/delivery`);
    const other = waitForRequest("post", `${API}/app-1/actions/act2/delivery`);
    expect(await adoptStartedGadgetSession("app-1", "s_new")).toBe(true);
    await acknowledgeStartedGadgetInput("app-1", "s_new", prompt);
    expect(await (await first).json()).toEqual({ session_id: "s_new" });
    expect(await (await other).json()).toEqual({ session_id: "s_new" });
  });

  it("links the started session without counting its requests delivered until its first message is accepted", async () => {
    const deliveries: string[] = [];
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: null, version: null })),
      ),
      rest.put(`${API}/app-1/sessions/:sessionId`, (_req, res, ctx) =>
        res(ctx.json({ items: [] })),
      ),
      rest.post(`${API}/app-1/actions/:actionId/delivery`, (req, res, ctx) => {
        deliveries.push((req as { path: string }).path.split("/actions/")[1].split("/")[0]);
        return res(ctx.json({ items: [] }));
      }),
    );
    await requestGadgetAction(EDITOR, { action: "run_card", params: { cardId: "c1" } }, navigate);
    const prompt = parse(navigate.mock.calls[0][0]).searchParams.get("prompt") ?? "";
    const linked = waitForRequest("put", `${API}/app-1/sessions/s_new`);
    expect(await adoptStartedGadgetSession("app-1", "s_new")).toBe(true);
    await linked;
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(deliveries).toEqual([]);
    await acknowledgeStartedGadgetInput("app-1", "s_new", prompt);
    await waitFor(() => expect(deliveries).toEqual(["act1"]));
  });

  it("acknowledges only the requests the session's first message carried", async () => {
    const second = { ...ENTRY, id: "act2", value: { ...ENTRY.value, params: { cardId: "c2" } } };
    let next = ENTRY;
    const deliveries: string[] = [];
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(next))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: null, version: null })),
      ),
      rest.put(`${API}/app-1/sessions/:sessionId`, (_req, res, ctx) =>
        res(ctx.json({ items: [] })),
      ),
      rest.post(`${API}/app-1/actions/:actionId/delivery`, (req, res, ctx) => {
        deliveries.push((req as { path: string }).path.split("/actions/")[1].split("/")[0]);
        return res(ctx.json({ items: [] }));
      }),
    );
    await requestGadgetAction(EDITOR, { action: "run_card", params: { cardId: "c1" } }, navigate);
    next = second;
    await requestGadgetAction(EDITOR, { action: "run_card", params: { cardId: "c2" } }, navigate);
    const prompt = parse(navigate.mock.calls[1][0]).searchParams.get("prompt") ?? "";

    // The viewer deleted the second request from New session's prompt before sending it.
    const edited = prompt.slice(0, prompt.indexOf('Gadget action\n{"action_id":"act2"'));
    const linked = waitForRequest("put", `${API}/app-1/sessions/s_new`);
    expect(await adoptStartedGadgetSession("app-1", "s_new")).toBe(true);
    await acknowledgeStartedGadgetInput("app-1", "s_new", edited);
    await linked;
    await waitFor(() => expect(deliveries).toEqual(["act1"]));

    // A session created without a known first message acknowledges nothing.
    deliveries.length = 0;
    next = ENTRY;
    await requestGadgetAction(EDITOR, { action: "run_card", params: { cardId: "c1" } }, navigate);
    expect(await adoptStartedGadgetSession("app-1", "s_other")).toBe(true);
    await acknowledgeStartedGadgetInput("app-1", "s_other", null);
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(deliveries).toEqual([]);
  });

  it("counts nothing delivered when the accepted first message is a different one, and keeps the requests queued", async () => {
    const deliveries: string[] = [];
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: null, version: null })),
      ),
      rest.put(`${API}/app-1/sessions/:sessionId`, (_req, res, ctx) =>
        res(ctx.json({ items: [] })),
      ),
      rest.post(`${API}/app-1/actions/:actionId/delivery`, (req, res, ctx) => {
        deliveries.push((req as { path: string }).path.split("/actions/")[1].split("/")[0]);
        return res(ctx.json({ items: [] }));
      }),
    );
    await requestGadgetAction(EDITOR, { action: "run_card", params: { cardId: "c1" } }, navigate);
    expect(await adoptStartedGadgetSession("app-1", "s_new")).toBe(true);
    // Its first send failed; the viewer took the request out and sent something else.
    expect(await acknowledgeStartedGadgetInput("app-1", "s_new", "Just say hi")).toEqual([]);
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(deliveries).toEqual([]);
    // Still queued: the next session started for the gadget carries it.
    navigate.mockClear();
    await requestGadgetAction(EDITOR, { action: "run_card", params: { cardId: "c1" } }, navigate);
    const prompt = parse(navigate.mock.calls[0][0]).searchParams.get("prompt") ?? "";
    expect(prompt).toContain('"action_id":"act1"');
  });

  it("returns the deliveries it couldn't record, after trying each again", async () => {
    let tries = 0;
    server.use(
      rest.post(`${API}/app-1/actions/act1/delivery`, (_req, res, ctx) => {
        tries += 1;
        return res(ctx.status(503), ctx.json({ error: { message: "busy" } }));
      }),
      rest.post(`${API}/app-1/actions/act2/delivery`, (_req, res, ctx) =>
        res(ctx.json({ items: [] })),
      ),
    );
    expect(await recordGadgetDeliveries("app-1", "s_new", ["act1", "act2"])).toEqual(["act1"]);
    expect(tries).toBe(3);
  }, 10_000);

  it("sends actions and quotes to the session the gadget is shown beside", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      // The session beside the gadget is the viewer's gadget session.
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_rail", version: "v" })),
      ),
      rest.post(`${API}/app-1/actions/act1/delivery`, (_req, res, ctx) =>
        res(ctx.json({ items: [] })),
      ),
    );
    const delivered = waitForRequest("post", `${API}/app-1/actions/act1/delivery`);

    const receipt = await requestGadgetAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" } },
      navigate,
      "s_rail",
    );
    await quoteInGadgetSession(EDITOR, "reply", "Fix login", navigate, "s_rail");

    expect(receipt).toEqual({ actionId: "act1", delivery: "sent" });
    expect(sendUserMessage).toHaveBeenCalledWith(
      "s_rail",
      expect.stringContaining("Gadget action"),
    );
    expect(await (await delivered).json()).toEqual({ session_id: "s_rail" });
    expect(queueGadgetComposerQuote).toHaveBeenCalledWith("s_rail", "reply", "Fix login");
    // The session is already on screen beside the gadget.
    expect(navigate).not.toHaveBeenCalled();
  });

  it("sends an action from beside a cited session to the viewer's gadget session", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_actions", version: "v" })),
      ),
      rest.post(`${API}/app-1/actions/act1/delivery`, (_req, res, ctx) =>
        res(ctx.json({ items: [] })),
      ),
    );
    const delivered = waitForRequest("post", `${API}/app-1/actions/act1/delivery`);

    // The gadget is shown beside s_cited, which its page cited; s_actions handles its actions.
    const receipt = await requestGadgetAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" } },
      navigate,
      "s_cited",
    );

    expect(receipt).toEqual({ actionId: "act1", delivery: "sent" });
    expect(sendUserMessage).toHaveBeenCalledTimes(1);
    expect(sendUserMessage).toHaveBeenCalledWith(
      "s_actions",
      expect.stringContaining("Gadget action"),
    );
    expect(await (await delivered).json()).toEqual({ session_id: "s_actions" });
    // The viewer stays on the session they are reading.
    expect(navigate).not.toHaveBeenCalled();
  });

  it("starts a gadget session instead of sending to a cited session when there is none", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) => res(ctx.json({ session_id: null }))),
    );

    const receipt = await requestGadgetAction(
      EDITOR,
      { action: "run_card", params: { cardId: "c1" } },
      navigate,
      "s_cited",
    );

    expect(receipt).toMatchObject({ delivery: "starting" });
    expect(sendUserMessage).not.toHaveBeenCalled();
    expect(parse(navigate.mock.calls[0][0]).pathname).toBe("/");
  });

  it("sends nothing when the gadget session can't be looked up beside a cited session", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.status(503), ctx.json({ error: { code: "unavailable", message: "Try later" } })),
      ),
    );

    await expect(
      requestGadgetAction(
        EDITOR,
        { action: "run_card", params: { cardId: "c1" } },
        navigate,
        "s_cited",
      ),
    ).rejects.toMatchObject({ code: "UNAVAILABLE" });
    expect(sendUserMessage).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("starts a new gadget session when the remembered one is gone", async () => {
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => res(ctx.status(201), ctx.json(ENTRY))),
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_gone", version: "v" })),
      ),
    );
    sendUserMessage.mockRejectedValueOnce(
      Object.assign(new Error("Session not found"), { status: 404 }),
    );

    const receipt = await requestGadgetAction(EDITOR, { action: "run_card", params: {} }, navigate);

    expect(receipt).toMatchObject({ delivery: "starting" });
    expect(parse(navigate.mock.calls[0][0]).pathname).toBe("/");
  });

  it("reports a gadget session that could not take the request, keeping the viewer's place", async () => {
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
      requestGadgetAction(EDITOR, { action: "run_card", params: {} }, navigate),
    ).rejects.toMatchObject({ code: "UNAVAILABLE" });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("quotes a selection in the gadget session composer, like Reply in a session", async () => {
    server.use(
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: "s_1", version: "v" })),
      ),
    );
    const quote = gadgetSelectionQuote(EDITOR, {
      text: "Fix login",
      rect: { x: 0, y: 0, width: 0, height: 0 },
      occurrence: 2,
      ref: "cards/c1#title",
      label: "Card",
    });

    await quoteInGadgetSession(EDITOR, "side_chat", quote, navigate);

    expect(quote).toBe(
      'Fix login\n— Card in "Team board" (gadget_id app-1, ref cards/c1#title, occurrence 2)',
    );
    expect(queueGadgetComposerQuote).toHaveBeenCalledWith("s_1", "side_chat", quote);
    // The gadget session opens with the gadget in its side panel.
    expect(requestGadgetRailTab).toHaveBeenCalledWith("s_1", "gadget:app-1");
    expect(parse(navigate.mock.calls[0][0]).pathname).toBe("/c/s_1");
  });

  it("starts the gadget session with the quote when the viewer has none", async () => {
    server.use(
      rest.get(`${API}/app-1/session`, (_req, res, ctx) =>
        res(ctx.json({ session_id: null, version: null })),
      ),
    );

    await quoteInGadgetSession(
      EDITOR,
      "reply",
      'Fix login\n— "Team board" (gadget_id app-1)',
      navigate,
    );

    expect(queueGadgetComposerQuote).not.toHaveBeenCalled();
    const opened = parse(navigate.mock.calls[0][0]);
    expect(opened.pathname).toBe("/");
    expect(opened.searchParams.get("prompt")).toContain(
      '> Fix login\n> — "Team board" (gadget_id app-1)',
    );
  });

  it("links the gadget to the session it opens, so its side panel shows the gadget", async () => {
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
    const unsubscribe = onSessionGadgetsChanged(changed);

    await openGadgetInSession("app-1", "s_1", navigate);

    expect(await (await linked).json()).toEqual({ linked: true });
    expect(changed).toHaveBeenCalledWith("s_1", [expect.objectContaining({ id: "app-1" })]);
    expect(requestGadgetRailTab).toHaveBeenCalledWith("s_1", "gadget:app-1");
    expect(navigate).toHaveBeenCalledWith("/c/s_1");
    unsubscribe();
  });

  it("still opens the session when the gadget cannot be linked there", async () => {
    server.use(
      rest.put(`${API}/app-1/sessions/:sessionId`, (_req, res, ctx) => res(ctx.status(403))),
    );

    await openGadgetInSession("app-1", "s_1", navigate);

    expect(navigate).toHaveBeenCalledWith("/c/s_1");
  });

  it("refuses a viewer who cannot edit the gadget", async () => {
    const actions = vi.fn();
    server.use(
      rest.post(`${API}/app-1/actions`, (_req, res, ctx) => {
        actions();
        return res(ctx.status(201), ctx.json(ENTRY));
      }),
    );

    await expect(
      requestGadgetAction(
        { ...EDITOR, permissions: { ...EDITOR.permissions, publish: false } },
        { action: "run_card", params: {} },
        navigate,
      ),
    ).rejects.toEqual(
      new BridgeError(
        "PERMISSION_DENIED",
        "Running actions on this gadget needs Can edit gadget access.",
      ),
    );
    expect(actions).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });
});
