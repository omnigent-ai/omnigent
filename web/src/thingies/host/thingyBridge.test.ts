import { describe, expect, it, vi } from "vitest";

import { waitFor } from "@testing-library/react";

import type {
  ThingyActionRequest,
  ThingyPageRequest,
  ThingyPort,
  StartThingyBridgeOptions,
} from "./thingyBridge";
import { BridgeError, startThingyBridge } from "./thingyBridge";
import { ThingyApiError } from "../api/thingyApi";
import type { ThingyInitContext } from "../sdk/thingyProtocol";
import { THINGY_LIMITS } from "../sdk/thingyProtocol";

type Listener = (event: MessageEvent) => void;

/** Host side of one port: records what the bridge posts and lets the test speak as the frame. */
class FakeHostPort implements ThingyPort {
  readonly posted: unknown[] = [];
  readonly listeners = new Set<Listener>();
  closed = false;

  postMessage(message: unknown): void {
    this.posted.push(message);
  }
  addEventListener(_type: "message", listener: Listener): void {
    this.listeners.add(listener);
  }
  removeEventListener(_type: "message", listener: Listener): void {
    this.listeners.delete(listener);
  }
  close(): void {
    this.closed = true;
  }
  fromFrame(data: unknown): void {
    this.listeners.forEach((listener) => listener(new MessageEvent("message", { data })));
  }
  responseFor(id: string): unknown {
    return this.posted.find(
      (message) =>
        typeof message === "object" && message !== null && "id" in message && message.id === id,
    );
  }
}

const CONTEXT: ThingyInitContext = {
  protocol: 1,
  app: { id: "app-1", name: "Team board" },
  revisionId: "rev-1",
  viewer: { id: "1001", displayName: "viewer@example.com" },
  capabilities: ["data.read", "data.write"],
  permissions: { writeData: true, publish: false },
  colorScheme: "light",
};

type CallRpc = StartThingyBridgeOptions["callRpc"];

function start(overrides: Partial<StartThingyBridgeOptions> = {}) {
  const port = new FakeHostPort();
  const callRpc = vi.fn<CallRpc>();
  const bridge = startThingyBridge({ port, context: CONTEXT, callRpc, ...overrides });
  return { port, callRpc: vi.mocked(overrides.callRpc ?? callRpc), bridge };
}

const request = (id: string, method: string, params: Record<string, unknown>) => ({
  v: 1,
  kind: "request",
  id,
  method,
  params,
  deadlineMs: 5_000,
});

const serverDocument = {
  id: "c1",
  value: { title: "x" },
  version: "v2",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
  created_by: "1001",
  updated_by: "1002",
};

const sdkDocument = {
  id: "c1",
  value: { title: "x" },
  version: "v2",
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-01-02T00:00:00Z",
  createdBy: "1001",
  updatedBy: "1002",
};

// The bridge is driven through a stand-in port; the RPC route is the network edge.
describe("startThingyBridge", () => {
  it("sends the context, then translates a list call to the route shape and the page back", async () => {
    const callRpc = vi
      .fn<CallRpc>()
      .mockResolvedValue({ items: [serverDocument], next_cursor: "cursor-2" });
    const { port } = start({ callRpc });

    expect(port.posted[0]).toEqual({ v: 1, kind: "init", context: CONTEXT });
    port.fromFrame(
      request("r1", "data.v1.list", { collection: "cards", limit: 10, cursor: "cursor-1" }),
    );

    await waitFor(() =>
      expect(port.responseFor("r1")).toEqual({
        v: 1,
        kind: "response",
        id: "r1",
        ok: true,
        result: { items: [sdkDocument], nextCursor: "cursor-2" },
      }),
    );
    expect(callRpc).toHaveBeenCalledWith(
      "data.v1.list",
      { collection: "cards", limit: 10, cursor: "cursor-1" },
      expect.any(AbortSignal),
    );
  });

  it("sends a replace with its expected version and operation ID in the route shape", async () => {
    const callRpc = vi.fn<CallRpc>().mockResolvedValue(serverDocument);
    const { port } = start({ callRpc });

    port.fromFrame(
      request("r1", "data.v1.replace", {
        collection: "cards",
        id: "c1",
        value: { title: "x" },
        ifVersion: "v1",
        operationId: "edit-1",
      }),
    );

    await waitFor(() =>
      expect(port.responseFor("r1")).toMatchObject({ ok: true, result: sdkDocument }),
    );
    expect(callRpc).toHaveBeenCalledWith(
      "data.v1.replace",
      {
        collection: "cards",
        id: "c1",
        value: { title: "x" },
        if_version: "v1",
        operation_id: "edit-1",
      },
      expect.any(AbortSignal),
    );
  });

  it("rejects an unexpected parameter without calling the server", async () => {
    const { port, callRpc } = start();

    port.fromFrame(
      request("r1", "data.v1.get", { collection: "cards", id: "c1", url: "https://example.com" }),
    );

    await waitFor(() =>
      expect(port.responseFor("r1")).toMatchObject({
        ok: false,
        error: { code: "INVALID_ARGUMENT" },
      }),
    );
    expect(callRpc).not.toHaveBeenCalled();
  });

  it("rejects a document value that is not supported JSON without calling the server", () => {
    const { port, callRpc } = start();

    port.fromFrame(
      request("r1", "data.v1.create", {
        collection: "cards",
        value: { due: new Date(0) },
        operationId: "o1",
      }),
    );

    expect(port.responseFor("r1")).toMatchObject({
      ok: false,
      error: { code: "INVALID_ARGUMENT" },
    });
    expect(callRpc).not.toHaveBeenCalled();
  });

  it("answers UNSUPPORTED for a capability the revision was not granted", () => {
    const { port, callRpc } = start({ context: { ...CONTEXT, capabilities: ["data.read"] } });

    port.fromFrame(
      request("r1", "data.v1.delete", {
        collection: "cards",
        id: "c1",
        ifVersion: "v1",
        operationId: "o1",
      }),
    );

    expect(port.responseFor("r1")).toMatchObject({ ok: false, error: { code: "UNSUPPORTED" } });
    expect(callRpc).not.toHaveBeenCalled();
  });

  it("refuses a request beyond the in-flight budget", () => {
    const callRpc = vi.fn<CallRpc>().mockImplementation(
      (_method, _params, signal) =>
        new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    );
    const { port, bridge } = start({ callRpc });

    for (let index = 0; index <= THINGY_LIMITS.maxInFlightRequests; index += 1) {
      port.fromFrame(request(`r${index}`, "data.v1.get", { collection: "cards", id: "c1" }));
    }

    expect(callRpc).toHaveBeenCalledTimes(THINGY_LIMITS.maxInFlightRequests);
    expect(port.responseFor(`r${THINGY_LIMITS.maxInFlightRequests}`)).toMatchObject({
      ok: false,
      error: { code: "RESOURCE_EXHAUSTED" },
    });
    bridge.dispose();
  });

  it("aborts the server call when the frame cancels and sends no response", async () => {
    let signal: AbortSignal | undefined;
    const callRpc = vi.fn<CallRpc>().mockImplementation((_method, _params, callSignal) => {
      signal = callSignal;
      return new Promise((_resolve, reject) => {
        callSignal.addEventListener("abort", () => reject(new Error("aborted")));
      });
    });
    const { port } = start({ callRpc });

    port.fromFrame(request("r1", "data.v1.get", { collection: "cards", id: "c1" }));
    port.fromFrame({ v: 1, kind: "cancel", id: "r1" });
    await Promise.resolve();

    expect(signal?.aborted).toBe(true);
    expect(port.responseFor("r1")).toBeUndefined();
  });

  it("reports a published revision change once and refuses later calls", async () => {
    const onRevisionChanged = vi.fn<() => void>();
    const callRpc = vi.fn<CallRpc>().mockRejectedValue(
      new ThingyApiError({
        status: 409,
        code: "CONFLICT",
        message: "The app was republished",
        details: { reason: "REVISION_CHANGED", published_revision: "rev-2" },
      }),
    );
    const { port } = start({ callRpc, onRevisionChanged });

    port.fromFrame(request("r1", "data.v1.get", { collection: "cards", id: "c1" }));
    await waitFor(() =>
      expect(port.responseFor("r1")).toMatchObject({
        ok: false,
        error: { code: "REVISION_CHANGED" },
      }),
    );
    port.fromFrame(request("r2", "data.v1.get", { collection: "cards", id: "c1" }));

    expect(port.responseFor("r2")).toMatchObject({
      ok: false,
      error: { code: "REVISION_CHANGED" },
    });
    expect(onRevisionChanged).toHaveBeenCalledTimes(1);
    expect(callRpc).toHaveBeenCalledTimes(1);
  });

  it("passes through a server error code it does not know instead of mapping the status", async () => {
    const callRpc = vi.fn<CallRpc>().mockRejectedValue(
      new ThingyApiError({
        status: 409,
        code: "DATA_LOSS",
        message: "Stored record failed validation",
      }),
    );
    const { port } = start({ callRpc });

    port.fromFrame(request("r1", "data.v1.get", { collection: "cards", id: "c1" }));

    await waitFor(() =>
      expect(port.responseFor("r1")).toMatchObject({
        ok: false,
        error: { code: "DATA_LOSS", message: "Stored record failed validation" },
      }),
    );
  });

  it("keeps the request ID out of the message the thingy shows", async () => {
    const callRpc = vi.fn<CallRpc>().mockRejectedValue(
      new ThingyApiError({
        status: 400,
        code: "CONSTRAINT_VIOLATION",
        message: "In progress holds at most 5 cards.",
        requestId: "18161a30-aaaa",
      }),
    );
    const { port } = start({ callRpc });

    port.fromFrame(request("r1", "data.v1.get", { collection: "cards", id: "c1" }));

    await waitFor(() =>
      expect(port.responseFor("r1")).toMatchObject({
        ok: false,
        error: {
          code: "CONSTRAINT_VIOLATION",
          message: "In progress holds at most 5 cards.",
          requestId: "18161a30-aaaa",
        },
      }),
    );
  });

  it.each([
    { label: "declines", confirmed: false, expectedCalls: 0 },
    { label: "allows", confirmed: true, expectedCalls: 1 },
  ])("creates a session only after the viewer $label", async ({ confirmed, expectedCalls }) => {
    const callRpc = vi.fn<CallRpc>().mockResolvedValue({ session_id: "s1" });
    const confirmSessionCreate = vi
      .fn<(params: Record<string, unknown>) => Promise<boolean>>()
      .mockResolvedValue(confirmed);
    const { port } = start({
      callRpc,
      confirmSessionCreate,
      context: { ...CONTEXT, capabilities: ["sessions.create"] },
    });

    port.fromFrame(
      request("r1", "sessions.v1.create", { initialPrompt: "hi", operationId: "op-1" }),
    );

    await waitFor(() =>
      expect(port.responseFor("r1")).toMatchObject(
        confirmed
          ? { ok: true, result: { sessionId: "s1" } }
          : { ok: false, error: { code: "PERMISSION_DENIED" } },
      ),
    );
    expect(confirmSessionCreate).toHaveBeenCalledWith({
      initial_prompt: "hi",
      operation_id: "op-1",
    });
    expect(callRpc).toHaveBeenCalledTimes(expectedCalls);
  });

  describe("navigation", () => {
    it("opens a New session page without any capability and without calling the server", () => {
      const openPage = vi.fn((_request: ThingyPageRequest) => true);
      const { port, callRpc } = start({ context: { ...CONTEXT, capabilities: [] }, openPage });

      port.fromFrame(request("r1", "navigation.v1.newSession", { prompt: "Summarize the card" }));

      expect(openPage).toHaveBeenCalledWith({
        kind: "newSession",
        prompt: "Summarize the card",
        display: "tab",
      });
      expect(port.responseFor("r1")).toEqual({
        v: 1,
        kind: "response",
        id: "r1",
        ok: true,
        result: { opened: true },
      });
      expect(callRpc).not.toHaveBeenCalled();
    });

    it("opens New session without a prompt when the thingy gives none", () => {
      const openPage = vi.fn((_request: ThingyPageRequest) => true);
      const { port } = start({ openPage });

      port.fromFrame(request("r1", "navigation.v1.newSession", {}));

      expect(openPage).toHaveBeenCalledWith({ kind: "newSession", prompt: "", display: "tab" });
      expect(port.responseFor("r1")).toMatchObject({ ok: true });
    });

    it("opens a prefilled automation with its RRULE schedule", () => {
      const openPage = vi.fn((_request: ThingyPageRequest) => true);
      const { port } = start({ openPage });

      port.fromFrame(
        request("r1", "navigation.v1.newAutomation", {
          name: " Daily refresh ",
          prompt: "Refresh the board",
          schedule: "FREQ=WEEKLY;BYDAY=MO,TU;BYHOUR=9;BYMINUTE=0",
        }),
      );

      expect(openPage).toHaveBeenCalledWith({
        kind: "newAutomation",
        name: "Daily refresh",
        prompt: "Refresh the board",
        schedule: "FREQ=WEEKLY;BYDAY=MO,TU;BYHOUR=9;BYMINUTE=0",
        display: "tab",
      });
      expect(port.responseFor("r1")).toMatchObject({ ok: true, result: { opened: true } });
    });

    it("opens a session by ID", () => {
      const openPage = vi.fn((_request: ThingyPageRequest) => true);
      const { port } = start({ openPage });

      port.fromFrame(request("r1", "navigation.v1.openSession", { sessionId: "abc_123-def" }));

      expect(openPage).toHaveBeenCalledWith({
        kind: "openSession",
        sessionId: "abc_123-def",
        display: "tab",
      });
      expect(port.responseFor("r1")).toMatchObject({ ok: true, result: { opened: true } });
    });

    it.each([
      [
        "navigation.v1.newSession",
        { prompt: "Refresh", display: "panel" },
        { kind: "newSession", display: "panel" },
      ],
      ["navigation.v1.newSession", { display: "page" }, { kind: "newSession", display: "page" }],
      [
        "navigation.v1.openSession",
        { sessionId: "s1", display: "panel" },
        { kind: "openSession", display: "panel" },
      ],
      [
        "navigation.v1.newAutomation",
        { name: "Daily", prompt: "Refresh", display: "page" },
        { kind: "newAutomation", display: "page" },
      ],
    ])("passes the display the thingy chose for %s", (method, params, expected) => {
      const openPage = vi.fn((_request: ThingyPageRequest) => true);
      const { port } = start({ openPage });

      port.fromFrame(request("r1", method, params));

      expect(openPage).toHaveBeenCalledWith(expect.objectContaining(expected));
      expect(port.responseFor("r1")).toMatchObject({ ok: true });
    });

    it.each([
      ["the host did not open the page", vi.fn((_request: ThingyPageRequest) => false)],
      ["the host cannot open pages", undefined],
    ])("answers PERMISSION_DENIED when %s", (_case, openPage) => {
      const { port } = start({ openPage });

      port.fromFrame(request("r1", "navigation.v1.newSession", { prompt: "Summarize the card" }));

      expect(port.responseFor("r1")).toMatchObject({
        ok: false,
        error: { code: "PERMISSION_DENIED" },
      });
    });

    it.each([
      [
        "a prompt over the length limit",
        "navigation.v1.newSession",
        { prompt: "x".repeat(THINGY_LIMITS.maxSessionPromptLength + 1) },
      ],
      [
        "an extra parameter",
        "navigation.v1.newSession",
        { prompt: "hi", url: "https://example.com" },
      ],
      ["a session ID with a path", "navigation.v1.openSession", { sessionId: "../settings" }],
      ["an unknown display", "navigation.v1.newSession", { prompt: "hi", display: "popup" }],
      [
        "an automation in a panel",
        "navigation.v1.newAutomation",
        { name: "Daily", prompt: "Hi", display: "panel" },
      ],
      [
        "an automation without a prompt",
        "navigation.v1.newAutomation",
        { name: "Daily", prompt: " " },
      ],
      [
        "an automation schedule that is not an RRULE",
        "navigation.v1.newAutomation",
        { name: "Daily", prompt: "Hi", schedule: "0 9 * * *" },
      ],
    ])("rejects %s without opening a page", (_case, method, params) => {
      const openPage = vi.fn((_request: ThingyPageRequest) => true);
      const { port } = start({ openPage });

      port.fromFrame(request("r1", method, params));

      expect(port.responseFor("r1")).toMatchObject({
        ok: false,
        error: { code: "INVALID_ARGUMENT" },
      });
      expect(openPage).not.toHaveBeenCalled();
    });
  });

  describe("actions", () => {
    it("hands a declared action to the host and answers with its receipt, without any capability", async () => {
      const requestAction = vi.fn((_request: ThingyActionRequest) =>
        Promise.resolve({ actionId: "a1", delivery: "sent" }),
      );
      const { port, callRpc } = start({ context: { ...CONTEXT, capabilities: [] }, requestAction });

      port.fromFrame(
        request("r1", "agent.v1.request", { action: "run_card", params: { cardId: "c1" } }),
      );

      await waitFor(() => expect(port.responseFor("r1")).toBeDefined());
      expect(requestAction).toHaveBeenCalledWith({ action: "run_card", params: { cardId: "c1" } });
      expect(port.responseFor("r1")).toEqual({
        v: 1,
        kind: "response",
        id: "r1",
        ok: true,
        result: { actionId: "a1", delivery: "sent" },
      });
      expect(callRpc).not.toHaveBeenCalled();
    });

    it("passes the host's refusal to the frame", async () => {
      const requestAction = vi.fn(() =>
        Promise.reject(
          new BridgeError(
            "PERMISSION_DENIED",
            "Running actions on this thingy needs Can edit thingy access.",
          ),
        ),
      );
      const { port } = start({ requestAction });

      port.fromFrame(request("r1", "agent.v1.request", { action: "run_card" }));

      await waitFor(() => expect(port.responseFor("r1")).toBeDefined());
      expect(port.responseFor("r1")).toMatchObject({
        ok: false,
        error: {
          code: "PERMISSION_DENIED",
          message: "Running actions on this thingy needs Can edit thingy access.",
        },
      });
    });

    it.each([
      ["an action name that is not an identifier", { action: "Run Card" }],
      ["params that are not an object", { action: "run_card", params: ["c1"] }],
      ["an extra parameter", { action: "run_card", session: "s1" }],
      ["params over the size limit", { action: "run_card", params: { notes: "x".repeat(9000) } }],
    ])("rejects %s without asking the host", async (_case, params) => {
      const requestAction = vi.fn(() => Promise.resolve({}));
      const { port } = start({ requestAction });

      port.fromFrame(request("r1", "agent.v1.request", params));

      await waitFor(() => expect(port.responseFor("r1")).toBeDefined());
      expect(port.responseFor("r1")).toMatchObject({
        ok: false,
        error: { code: "INVALID_ARGUMENT" },
      });
      expect(requestAction).not.toHaveBeenCalled();
    });

    it("answers UNSUPPORTED when the host cannot run actions", async () => {
      const { port } = start({});

      port.fromFrame(request("r1", "agent.v1.request", { action: "run_card" }));

      await waitFor(() => expect(port.responseFor("r1")).toBeDefined());
      expect(port.responseFor("r1")).toMatchObject({ ok: false, error: { code: "UNSUPPORTED" } });
    });

    it("reads action progress from the host for the given actions or the viewer’s tracked ones", async () => {
      const item = { id: "a1", status: "running" };
      const actionProgress = vi.fn((_ids: string[], _signal: AbortSignal) =>
        Promise.resolve([item]),
      );
      const { port, callRpc } = start({
        context: { ...CONTEXT, capabilities: [] },
        actionProgress,
      });

      port.fromFrame(request("r1", "agent.v1.progress", { actionIds: ["a1"] }));
      port.fromFrame(request("r2", "agent.v1.progress", {}));

      await waitFor(() => expect(port.responseFor("r2")).toBeDefined());
      expect(port.responseFor("r1")).toMatchObject({ ok: true, result: { items: [item] } });
      expect(actionProgress.mock.calls.map((call) => call[0])).toEqual([["a1"], []]);
      expect(callRpc).not.toHaveBeenCalled();
    });

    it("passes what the viewer selected, and its clearing, to the host without any capability", async () => {
      const onSelection = vi.fn();
      const { port, callRpc } = start({ context: { ...CONTEXT, capabilities: [] }, onSelection });
      const selection = {
        text: "Fix login",
        rect: { x: 10, y: 20, width: 80, height: 16 },
        occurrence: 2,
        ref: "cards/c1#title",
        label: "Card",
      };

      port.fromFrame(request("r1", "ui.v1.selection", { selection }));
      port.fromFrame(request("r2", "ui.v1.selection", { selection: null }));

      await waitFor(() => expect(port.responseFor("r2")).toBeDefined());
      expect(onSelection.mock.calls).toEqual([[selection], [null]]);
      expect(port.responseFor("r1")).toMatchObject({ ok: true, result: { shown: true } });
      expect(callRpc).not.toHaveBeenCalled();
    });

    it.each([
      ["empty text", { text: "  ", rect: { x: 0, y: 0, width: 0, height: 0 }, occurrence: 1 }],
      [
        "a rect that is not numbers",
        { text: "a", rect: { x: "left", y: 0, width: 0, height: 0 }, occurrence: 1 },
      ],
      [
        "an occurrence below one",
        { text: "a", rect: { x: 0, y: 0, width: 0, height: 0 }, occurrence: 0 },
      ],
    ])("rejects a selection with %s", async (_case, selection) => {
      const onSelection = vi.fn();
      const { port } = start({ onSelection });

      port.fromFrame(request("r1", "ui.v1.selection", { selection }));

      await waitFor(() => expect(port.responseFor("r1")).toBeDefined());
      expect(port.responseFor("r1")).toMatchObject({
        ok: false,
        error: { code: "INVALID_ARGUMENT" },
      });
      expect(onSelection).not.toHaveBeenCalled();
    });

    it.each([
      ["IDs that are not a list", { actionIds: "a1" }],
      ["an ID that is not an action ID", { actionIds: ["A 1"] }],
      ["more than 20 IDs", { actionIds: Array.from({ length: 21 }, (_, index) => `a${index}`) }],
    ])("rejects progress for %s", async (_case, params) => {
      const actionProgress = vi.fn(() => Promise.resolve([]));
      const { port } = start({ actionProgress });

      port.fromFrame(request("r1", "agent.v1.progress", params));

      await waitFor(() => expect(port.responseFor("r1")).toBeDefined());
      expect(port.responseFor("r1")).toMatchObject({
        ok: false,
        error: { code: "INVALID_ARGUMENT" },
      });
      expect(actionProgress).not.toHaveBeenCalled();
    });
  });

  describe("session list", () => {
    const session = {
      id: "s1",
      title: "Fix login",
      status: "running",
      harness: "claude-native",
      agentName: null,
      createdAt: 1,
      updatedAt: 2,
      unread: false,
      needsInput: true,
      backgroundWork: false,
    };
    const READ_CONTEXT = {
      ...CONTEXT,
      capabilities: ["data.read" as const, "sessions.read" as const],
    };

    it("answers from the host with the viewer list, not the RPC route", async () => {
      const listSessions = vi
        .fn<NonNullable<StartThingyBridgeOptions["listSessions"]>>()
        .mockResolvedValue({
          items: [session],
          nextCursor: null,
        });
      const { port, callRpc } = start({ context: READ_CONTEXT, listSessions });

      port.fromFrame(request("r1", "sessions.v1.list", { limit: 10 }));

      await waitFor(() =>
        expect(port.responseFor("r1")).toMatchObject({
          ok: true,
          result: { items: [session], nextCursor: null },
        }),
      );
      expect(listSessions).toHaveBeenCalledWith({ limit: 10 }, expect.any(AbortSignal));
      expect(callRpc).not.toHaveBeenCalled();
    });

    it("answers PERMISSION_DENIED when the viewer does not allow it", async () => {
      const listSessions = vi
        .fn<NonNullable<StartThingyBridgeOptions["listSessions"]>>()
        .mockResolvedValue(null);
      const { port } = start({ context: READ_CONTEXT, listSessions });

      port.fromFrame(request("r1", "sessions.v1.list", {}));

      await waitFor(() =>
        expect(port.responseFor("r1")).toMatchObject({
          ok: false,
          error: { code: "PERMISSION_DENIED" },
        }),
      );
      expect(listSessions).toHaveBeenCalledWith({ limit: 50 }, expect.any(AbortSignal));
    });

    it.each([
      ["without the sessions.read capability", CONTEXT, { limit: 10 }, "UNSUPPORTED"],
      [
        "with a limit over the page size",
        READ_CONTEXT,
        { limit: THINGY_LIMITS.maxListLimit + 1 },
        "INVALID_ARGUMENT",
      ],
    ])("refuses a list %s without asking the host", async (_case, context, params, code) => {
      const listSessions = vi.fn<NonNullable<StartThingyBridgeOptions["listSessions"]>>();
      const { port } = start({ context, listSessions });

      port.fromFrame(request("r1", "sessions.v1.list", params));

      await waitFor(() =>
        expect(port.responseFor("r1")).toMatchObject({ ok: false, error: { code } }),
      );
      expect(listSessions).not.toHaveBeenCalled();
    });
  });

  it("closes the port after too many malformed messages", () => {
    const onClosed = vi.fn<NonNullable<StartThingyBridgeOptions["onClosed"]>>();
    const { port } = start({ onClosed });

    for (let index = 0; index <= THINGY_LIMITS.maxProtocolViolations; index += 1) {
      port.fromFrame({ garbage: index });
    }

    expect(port.closed).toBe(true);
    expect(onClosed).toHaveBeenCalledWith("protocol_violations");
  });
});
