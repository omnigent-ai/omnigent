import { runInNewContext } from "node:vm";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GadgetInitContext } from "./gadgetProtocol";
import { GADGET_CONNECT_MESSAGE_TYPE, GADGET_LIMITS, parseFrameMessage } from "./gadgetProtocol";
import { buildOmnigentSdkBootstrapScript } from "./omnigentSdkRuntimeSource";
import type {
  OmnigentActionReceipt,
  OmnigentClient,
  OmnigentSdk,
  OmnigentWatchSnapshot,
} from "./omnigentSdkTypes";

interface PortMessage {
  data: unknown;
}

/** In-memory stand-in for one MessagePort; delivery is asynchronous like the real one. */
class FakePort {
  peer: FakePort | null = null;
  onmessage: ((event: PortMessage) => void) | null = null;
  readonly posted: unknown[] = [];
  closed = false;
  /** Makes the next `failSends` sends throw synchronously, as a real port does for uncloneable data. */
  sendError: Error | null = null;
  failSends = 0;

  postMessage(data: unknown): void {
    if (this.failSends > 0 && this.sendError) {
      this.failSends -= 1;
      throw this.sendError;
    }
    this.posted.push(data);
    const target = this.peer;
    void Promise.resolve().then(() => {
      if (target && !target.closed) {
        target.onmessage?.({ data });
      }
    });
  }

  close(): void {
    this.closed = true;
  }
}

function createPortPair(): [FakePort, FakePort] {
  const host = new FakePort();
  const frame = new FakePort();
  host.peer = frame;
  frame.peer = host;
  return [host, frame];
}

interface WindowMessage {
  source: unknown;
  data: unknown;
  ports: FakePort[];
}

interface FakeFrameWindow {
  parent: { postMessage: (data: unknown) => void; parent?: unknown };
  addEventListener: (type: string, listener: (event: WindowMessage) => void) => void;
  removeEventListener: (type: string, listener: (event: WindowMessage) => void) => void;
  omnigent?: OmnigentSdk;
}

const FRAME_ID = "omnigent-gadget-test";
const NONCE = "nonce-123";

const CONTEXT: GadgetInitContext = {
  protocol: 1,
  app: { id: "app-1", name: "Team board" },
  revisionId: "rev-1",
  viewer: { id: "1001", displayName: "viewer@example.com" },
  capabilities: ["data.read", "data.write"],
  permissions: { writeData: true, publish: false },
  colorScheme: "dark",
};

/**
 * Runs the exact bootstrap script the host injects, against a stand-in frame window. The
 * frame's real globals (a cross-origin sandbox) do not exist in jsdom, so the test supplies
 * the window the script talks to and plays the host's side of the handshake.
 */
function loadRuntime(
  dom: {
    document?: Document;
    MutationObserver?: typeof MutationObserver;
    globals?: Record<string, unknown>;
  } = {},
) {
  const listeners = new Set<(event: WindowMessage) => void>();
  const parentPosted: unknown[] = [];
  const parent: FakeFrameWindow["parent"] = { postMessage: (data) => parentPosted.push(data) };
  parent.parent = parent;
  const frameWindow: FakeFrameWindow = {
    ...dom.globals,
    parent,
    addEventListener: (_type, listener) => listeners.add(listener),
    removeEventListener: (_type, listener) => listeners.delete(listener),
  };
  const script = buildOmnigentSdkBootstrapScript({ frameId: FRAME_ID, nonce: NONCE });
  // The SDK ships as source text injected into the frame; evaluate that text, not a re-import.
  runInNewContext(script, {
    window: frameWindow,
    document: dom.document ?? { visibilityState: "visible" },
    MutationObserver: dom.MutationObserver,
    Object,
    Date,
    Promise,
    Error,
    Map,
    Set,
    Uint8Array,
    AbortController,
    crypto,
    TextEncoder,
    setTimeout,
    clearTimeout,
  });
  const sdk = frameWindow.omnigent;
  if (!sdk) {
    throw new Error("The runtime did not install window.omnigent");
  }
  const deliver = (event: WindowMessage) => listeners.forEach((listener) => listener(event));
  const connectHost = (context: GadgetInitContext = CONTEXT) => {
    const [hostPort, framePort] = createPortPair();
    deliver({
      source: parent,
      data: { type: GADGET_CONNECT_MESSAGE_TYPE, frameId: FRAME_ID, nonce: NONCE },
      ports: [framePort],
    });
    hostPort.postMessage({ v: 1, kind: "init", context });
    return hostPort;
  };
  return { sdk, frameWindow, parent, parentPosted, deliver, connectHost };
}

type Runtime = ReturnType<typeof loadRuntime>;

// Uploaded apps call the SDK from untyped JavaScript; Reflect.apply calls it the same way.
const callUntyped = (method: (...args: never[]) => unknown, target: unknown, args: unknown[]) =>
  Reflect.apply(method, target, args);

async function connected(runtime: Runtime, context?: GadgetInitContext) {
  const hostPort = runtime.connectHost(context);
  const client = await runtime.sdk.connect();
  return { hostPort, client };
}

function requestsOn(hostPort: FakePort, frameSentBefore: number): unknown[] {
  // The frame's messages arrive in the frame port's log, reachable through the pair.
  return hostPort.peer ? hostPort.peer.posted.slice(frameSentBefore) : [];
}

function respond(hostPort: FakePort, response: Record<string, unknown>): void {
  hostPort.postMessage({ v: 1, kind: "response", ...response });
}

function lastFrameMessage(hostPort: FakePort): unknown {
  const sent = hostPort.peer?.posted ?? [];
  return sent[sent.length - 1];
}

function lastRequestId(hostPort: FakePort): string {
  const last = lastFrameMessage(hostPort);
  if (typeof last !== "object" || last === null || !("id" in last) || typeof last.id !== "string") {
    throw new Error("The frame has not sent a request");
  }
  return last.id;
}

// The injected SDK source is evaluated and fed host messages; each case asserts what it computes.
describe("omnigent SDK runtime", () => {
  it("removes WebRTC, which the frame's CSP does not cover, before the gadget's script runs", () => {
    const FakePeerConnection = function FakePeerConnection() {};
    const { frameWindow } = loadRuntime({
      globals: {
        RTCPeerConnection: FakePeerConnection,
        webkitRTCPeerConnection: FakePeerConnection,
      },
    });
    const globals = frameWindow as unknown as Record<string, unknown>;

    expect(globals.RTCPeerConnection).toBeUndefined();
    expect(globals.webkitRTCPeerConnection).toBeUndefined();
    // The gadget cannot put it back.
    expect(Reflect.set(globals, "RTCPeerConnection", FakePeerConnection)).toBe(false);
    expect(globals.RTCPeerConnection).toBeUndefined();
  });

  describe("connect", () => {
    it("greets the host and resolves with the context from a trusted connect", async () => {
      const runtime = loadRuntime();
      expect(runtime.parentPosted).toEqual([
        { type: "omnigentGadget:hello", frameId: FRAME_ID, nonce: NONCE },
      ]);

      const { client } = await connected(runtime);

      expect(client.context).toEqual({
        app: { id: "app-1", name: "Team board" },
        revisionId: "rev-1",
        viewer: { id: "1001", displayName: "viewer@example.com" },
        permissions: { writeData: true, publish: false },
        preview: false,
      });
      expect(client.capabilities).toEqual(["data.read", "data.write"]);
      expect(client.ui.colorScheme).toBe("dark");
      expect("create" in client.sessions).toBe(false);
      expect(Object.isFrozen(client.context)).toBe(true);
    });

    describe("with fake timers", () => {
      beforeEach(() => {
        vi.useFakeTimers();
      });
      afterEach(() => {
        vi.useRealTimers();
      });

      it.each([
        { label: "a wrong nonce", source: "parent", nonce: "other" },
        { label: "an untrusted source", source: "stranger", nonce: NONCE },
      ])("ignores a connect with $label and times out UNAVAILABLE", async ({ source, nonce }) => {
        const runtime = loadRuntime();
        const [, framePort] = createPortPair();
        runtime.deliver({
          source: source === "parent" ? runtime.parent : {},
          data: { type: GADGET_CONNECT_MESSAGE_TYPE, frameId: FRAME_ID, nonce },
          ports: [framePort],
        });
        const result = runtime.sdk.connect({ timeoutMs: 500 });
        await Promise.all([
          expect(result).rejects.toMatchObject({ name: "OmnigentError", code: "UNAVAILABLE" }),
          vi.advanceTimersByTimeAsync(500),
        ]);
        expect(framePort.onmessage).toBeNull();
      });
    });
  });

  describe("calls", () => {
    it("rejects a call whose capability is not granted with UNSUPPORTED and sends nothing", async () => {
      const runtime = loadRuntime();
      const { hostPort, client } = await connected(runtime, {
        ...CONTEXT,
        capabilities: ["data.read"],
      });
      const sentBefore = hostPort.peer?.posted.length ?? 0;

      await expect(client.data.collection("cards").create({ title: "x" })).rejects.toMatchObject({
        code: "UNSUPPORTED",
      });
      expect(requestsOn(hostPort, sentBefore)).toEqual([]);
    });

    it("sends navigation calls with omitted options that the host accepts", async () => {
      const runtime = loadRuntime();
      const { hostPort, client } = await connected(runtime, { ...CONTEXT, capabilities: [] });
      // What the host receives is a structured clone, which keeps undefined properties; each
      // call must still pass the host's own parser.
      const accepted = () => parseFrameMessage(structuredClone(lastFrameMessage(hostPort)));
      void client.navigation.newSession({ prompt: "hi" }).catch(() => undefined);
      expect(accepted()).toMatchObject({ ok: true });
      void client.navigation.openSession("s1").catch(() => undefined);
      expect(accepted()).toMatchObject({ ok: true });
      void client.navigation
        .newAutomation({ name: "Daily", prompt: "Check it", schedule: "FREQ=DAILY" })
        .catch(() => undefined);
      expect(accepted()).toMatchObject({ ok: true });
      expect(lastFrameMessage(hostPort)).not.toHaveProperty("params.display");
    });

    it("sends navigation calls without any granted capability and resolves with the host result", async () => {
      const runtime = loadRuntime();
      const { hostPort, client } = await connected(runtime, { ...CONTEXT, capabilities: [] });

      const opened = client.navigation.newSession({ prompt: "Summarize the card" });
      expect(lastFrameMessage(hostPort)).toMatchObject({
        kind: "request",
        method: "navigation.v1.newSession",
        params: { prompt: "Summarize the card" },
      });
      respond(hostPort, { id: lastRequestId(hostPort), ok: true, result: { opened: true } });
      await expect(opened).resolves.toEqual({ opened: true });

      const session = client.navigation.openSession("s1");
      expect(lastFrameMessage(hostPort)).toMatchObject({
        method: "navigation.v1.openSession",
        params: { sessionId: "s1" },
      });
      respond(hostPort, {
        id: lastRequestId(hostPort),
        ok: false,
        error: { code: "PERMISSION_DENIED", message: "no click" },
      });
      await expect(session).rejects.toMatchObject({ code: "PERMISSION_DENIED", retryable: false });

      const panel = client.navigation.newSession({ prompt: "Refresh", display: "panel" });
      expect(lastFrameMessage(hostPort)).toMatchObject({
        params: { prompt: "Refresh", display: "panel" },
      });
      respond(hostPort, { id: lastRequestId(hostPort), ok: true, result: { opened: true } });
      await expect(panel).resolves.toEqual({ opened: true });

      const besideBoard = client.navigation.openSession("s2", { display: "panel" });
      expect(lastFrameMessage(hostPort)).toMatchObject({
        params: { sessionId: "s2", display: "panel" },
      });
      respond(hostPort, { id: lastRequestId(hostPort), ok: true, result: { opened: true } });
      await expect(besideBoard).resolves.toEqual({ opened: true });

      const action = client.agent.request("run_card", { cardId: "c1" });
      expect(lastFrameMessage(hostPort)).toMatchObject({
        method: "agent.v1.request",
        params: { action: "run_card", params: { cardId: "c1" }, operationId: expect.any(String) },
      });
      respond(hostPort, {
        id: lastRequestId(hostPort),
        ok: true,
        result: { actionId: "a1", delivery: "sent" },
      });
      const receipt = await action;
      expect(receipt).toMatchObject({ actionId: "a1", id: "a1", delivery: "sent" });
      expect(typeof receipt.watch).toBe("function");

      const automation = client.navigation.newAutomation({
        name: "Daily",
        prompt: "Refresh",
        schedule: "FREQ=DAILY",
      });
      expect(lastFrameMessage(hostPort)).toMatchObject({
        method: "navigation.v1.newAutomation",
        params: { name: "Daily", prompt: "Refresh", schedule: "FREQ=DAILY" },
      });
      respond(hostPort, { id: lastRequestId(hostPort), ok: true, result: { opened: true } });
      await expect(automation).resolves.toEqual({ opened: true });
    });

    it("sends the idempotency key as the operation ID and resolves with the host result", async () => {
      const runtime = loadRuntime();
      const { hostPort, client } = await connected(runtime);
      const document = {
        id: "c1",
        value: { title: "x" },
        version: "v1",
        createdAt: "2026-01-01T00:00:00Z",
        updatedAt: "2026-01-01T00:00:00Z",
        createdBy: "1001",
        updatedBy: "1001",
      };

      const result = client.data
        .collection("cards")
        .create({ title: "x" }, { idempotencyKey: "add-card-1" });
      expect(lastFrameMessage(hostPort)).toEqual({
        v: 1,
        kind: "request",
        id: expect.any(String),
        method: "data.v1.create",
        params: { collection: "cards", value: { title: "x" }, operationId: "add-card-1" },
        deadlineMs: 15_000,
      });
      respond(hostPort, { id: lastRequestId(hostPort), ok: true, result: document });

      await expect(result).resolves.toEqual(document);
    });

    it("refuses an idempotency key the host would refuse", async () => {
      const runtime = loadRuntime();
      const { client } = await connected(runtime);
      await Promise.all(
        ["_starts-with-punctuation", "has space", ""].map((key) =>
          expect(
            client.data.collection("cards").create({ title: "x" }, { idempotencyKey: key }),
          ).rejects.toMatchObject({ code: "INVALID_ARGUMENT" }),
        ),
      );
    });

    it("preserves an error code the SDK does not know", async () => {
      const runtime = loadRuntime();
      const { hostPort, client } = await connected(runtime);

      const result = client.data.collection("cards").get("c1");
      respond(hostPort, {
        id: lastRequestId(hostPort),
        ok: false,
        error: { code: "FUTURE_CODE", message: "Stored record failed validation" },
      });

      await expect(result).rejects.toMatchObject({
        code: "FUTURE_CODE",
        message: "Stored record failed validation",
        retryable: false,
      });
    });

    it("exposes the server's request ID on the error, apart from its message", async () => {
      const runtime = loadRuntime();
      const { hostPort, client } = await connected(runtime);

      const result = client.data.collection("cards").get("c1");
      respond(hostPort, {
        id: lastRequestId(hostPort),
        ok: false,
        error: { code: "NOT_FOUND", message: "No such card", requestId: "req-1" },
      });

      await expect(result).rejects.toMatchObject({
        code: "NOT_FOUND",
        message: "No such card",
        requestId: "req-1",
      });
    });

    it("cancels and rejects CANCELLED when the caller aborts", async () => {
      const runtime = loadRuntime();
      const { hostPort, client } = await connected(runtime);
      const controller = new AbortController();

      const result = client.data.collection("cards").get("c1", { signal: controller.signal });
      const requestId = lastRequestId(hostPort);
      controller.abort();

      await expect(result).rejects.toMatchObject({ code: "CANCELLED" });
      expect(lastFrameMessage(hostPort)).toEqual({ v: 1, kind: "cancel", id: requestId });
    });

    describe("with fake timers", () => {
      beforeEach(() => {
        vi.useFakeTimers();
      });
      afterEach(() => {
        vi.useRealTimers();
      });

      it("times out a mutation with DEADLINE_EXCEEDED, cancels it, and keeps its operation ID for a retry", async () => {
        const runtime = loadRuntime();
        const { hostPort, client } = await connected(runtime);

        const result = client.data
          .collection("cards")
          .replace(
            "c1",
            { title: "y" },
            { ifVersion: "v1", idempotencyKey: "edit-1", timeoutMs: 1_000 },
          );
        const requestId = lastRequestId(hostPort);
        await Promise.all([
          expect(result).rejects.toMatchObject({
            code: "DEADLINE_EXCEEDED",
            operationId: "edit-1",
            retryable: false,
          }),
          vi.advanceTimersByTimeAsync(1_000),
        ]);
        expect(lastFrameMessage(hostPort)).toEqual({ v: 1, kind: "cancel", id: requestId });
      });
    });
  });

  describe("input integrity", () => {
    it.each([
      { label: "NaN", value: { order: Number.NaN } },
      { label: "Infinity", value: { order: Number.POSITIVE_INFINITY } },
      { label: "undefined", value: { owner: undefined } },
      { label: "a Date", value: { due: new Date(0) } },
    ])(
      "rejects a document containing $label with INVALID_ARGUMENT before sending",
      async ({ value }) => {
        const runtime = loadRuntime();
        const { hostPort, client } = await connected(runtime);
        const cards = client.data.collection("cards");
        const sentBefore = hostPort.peer?.posted.length ?? 0;

        await expect(callUntyped(cards.create, cards, [value])).rejects.toMatchObject({
          code: "INVALID_ARGUMENT",
        });
        await expect(
          callUntyped(cards.replace, cards, ["c1", value, { ifVersion: "v1" }]),
        ).rejects.toMatchObject({
          code: "INVALID_ARGUMENT",
        });
        expect(requestsOn(hostPort, sentBefore)).toEqual([]);
      },
    );

    it.each([
      {
        label: "an uncloneable value",
        error: new DOMException("Could not clone", "DataCloneError"),
        code: "INVALID_ARGUMENT",
      },
      { label: "a closed port", error: new Error("Port is closed"), code: "UNAVAILABLE" },
    ])("reports a send that throws for $label as $code", async ({ error, code }) => {
      const runtime = loadRuntime();
      const { hostPort, client } = await connected(runtime);
      const framePort = hostPort.peer;
      if (!framePort) {
        throw new Error("The frame port is not connected");
      }
      framePort.sendError = error;
      framePort.failSends = 1;

      await expect(client.data.collection("cards").get("c1")).rejects.toMatchObject({
        name: "OmnigentError",
        code,
      });
    });

    it("forwards a cursor and a version longer than an identifier unchanged", async () => {
      const runtime = loadRuntime();
      const { hostPort, client } = await connected(runtime);
      const cards = client.data.collection("cards");
      const cursor = "c".repeat(GADGET_LIMITS.maxIdentifierLength * 3);
      const ifVersion = "v".repeat(GADGET_LIMITS.maxIdentifierLength * 3);

      // Both calls are cancelled and awaited, so none outlives the test.
      const controller = new AbortController();
      const listed = cards.list({ cursor }, { signal: controller.signal });
      expect(lastFrameMessage(hostPort)).toMatchObject({
        method: "data.v1.list",
        params: { cursor },
      });
      const replaced = cards.replace(
        "c1",
        { title: "x" },
        { ifVersion, signal: controller.signal },
      );
      expect(lastFrameMessage(hostPort)).toMatchObject({
        method: "data.v1.replace",
        params: { ifVersion },
      });
      controller.abort();
      await expect(listed).rejects.toMatchObject({ code: "CANCELLED" });
      await expect(replaced).rejects.toMatchObject({ code: "CANCELLED" });
    });

    describe("with fake timers", () => {
      beforeEach(() => {
        vi.useFakeTimers();
      });
      afterEach(() => {
        vi.useRealTimers();
      });

      it("releases the slot and deadline of each send that throws", async () => {
        const runtime = loadRuntime();
        const { hostPort, client } = await connected(runtime);
        const framePort = hostPort.peer;
        if (!framePort) {
          throw new Error("The frame port is not connected");
        }
        const cards = client.data.collection("cards");
        framePort.sendError = new Error("Port is closed");
        framePort.failSends = GADGET_LIMITS.maxInFlightRequests;

        for (let attempt = 0; attempt < GADGET_LIMITS.maxInFlightRequests; attempt += 1) {
          // Each failed send must settle before the next call, so the calls run one at a time.
          // oxlint-disable-next-line no-await-in-loop
          await expect(cards.get("c1", { timeoutMs: 1_000 })).rejects.toMatchObject({
            code: "UNAVAILABLE",
          });
        }
        const sentAfterFailures = framePort.posted.length;
        await vi.advanceTimersByTimeAsync(5_000);
        expect(framePort.posted).toHaveLength(sentAfterFailures);

        void cards.get("c2");
        expect(lastFrameMessage(hostPort)).toMatchObject({
          kind: "request",
          method: "data.v1.get",
          params: { id: "c2" },
        });
      });
    });
  });

  describe("watch", () => {
    let client: OmnigentClient;
    let hostPort: FakePort;
    const snapshots: OmnigentWatchSnapshot[] = [];

    beforeEach(async () => {
      snapshots.length = 0;
      vi.useFakeTimers();
      const runtime = loadRuntime();
      ({ client, hostPort } = await connected(runtime));
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    const listRequests = () =>
      (hostPort.peer?.posted ?? []).filter(
        (message) =>
          typeof message === "object" &&
          message !== null &&
          "method" in message &&
          message.method === "data.v1.list",
      );

    it("reports loading, then the first page live, and polls no faster than once a second", async () => {
      client.data
        .collection("cards")
        .watch((snapshot) => snapshots.push(snapshot), { intervalMs: 10 });
      respond(hostPort, {
        id: lastRequestId(hostPort),
        ok: true,
        result: { items: [], nextCursor: null },
      });
      await vi.advanceTimersByTimeAsync(0);

      expect(snapshots.map((snapshot) => snapshot.status)).toEqual(["loading", "live"]);
      await vi.advanceTimersByTimeAsync(999);
      expect(listRequests()).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(listRequests()).toHaveLength(2);
    });

    it("reports reconnecting after a failed poll, stale after repeated failures, and stops when disposed", async () => {
      const watch = client.data
        .collection("cards")
        .watch((snapshot) => snapshots.push(snapshot), { intervalMs: 1_000 });
      const failPoll = async (advanceMs: number) => {
        await vi.advanceTimersByTimeAsync(advanceMs);
        respond(hostPort, {
          id: lastRequestId(hostPort),
          ok: false,
          error: { code: "UNAVAILABLE", message: "down" },
        });
        await vi.advanceTimersByTimeAsync(0);
      };

      await failPoll(0);
      expect(snapshots[snapshots.length - 1]).toMatchObject({ status: "reconnecting", items: [] });
      await failPoll(2_000);
      await failPoll(4_000);
      expect(snapshots[snapshots.length - 1]).toMatchObject({
        status: "stale",
        error: { code: "UNAVAILABLE" },
      });

      watch.dispose();
      const requestsAtDispose = listRequests().length;
      await vi.advanceTimersByTimeAsync(60_000);
      expect(listRequests()).toHaveLength(requestsAtDispose);
    });

    it("follows the viewer's action progress, and reads it again at once on refresh", async () => {
      const progressRequests = () =>
        (hostPort.peer?.posted ?? []).filter(
          (message) =>
            typeof message === "object" &&
            message !== null &&
            "method" in message &&
            message.method === "agent.v1.progress",
        );
      const updates: { status: string; items: unknown[] }[] = [];
      const watch = client.agent.watch((snapshot) => updates.push(snapshot), { intervalMs: 3_000 });
      expect(lastFrameMessage(hostPort)).toMatchObject({
        method: "agent.v1.progress",
        params: { actionIds: [] },
      });
      const item = {
        id: "a1",
        status: "running",
        reason: null,
        action: "run_card",
        params: {},
        summary: null,
      };
      respond(hostPort, { id: lastRequestId(hostPort), ok: true, result: { items: [item] } });
      await vi.advanceTimersByTimeAsync(0);
      expect(updates.map((update) => update.status)).toEqual(["loading", "live"]);
      expect(updates[1].items).toEqual([item]);

      watch.refresh();
      expect(progressRequests()).toHaveLength(2);
      watch.dispose();
      await vi.advanceTimersByTimeAsync(60_000);
      expect(progressRequests()).toHaveLength(2);
    });

    it("keeps one poll running when its listener refreshes the watch", async () => {
      const progressRequests = () =>
        (hostPort.peer?.posted ?? []).filter(
          (message) =>
            typeof message === "object" &&
            message !== null &&
            "method" in message &&
            message.method === "agent.v1.progress",
        );
      let refreshed = false;
      let watch: { refresh: () => void; dispose: () => void } | null = null;
      watch = client.agent.watch(
        (snapshot) => {
          if (snapshot.status === "live" && !refreshed) {
            refreshed = true;
            watch?.refresh();
          }
        },
        { intervalMs: 3_000 },
      );
      respond(hostPort, { id: lastRequestId(hostPort), ok: true, result: { items: [] } });
      await vi.advanceTimersByTimeAsync(0);
      // The listener's refresh asked again; that answer is still on its way.
      expect(progressRequests()).toHaveLength(2);
      await vi.advanceTimersByTimeAsync(10_000);
      expect(progressRequests()).toHaveLength(2);

      respond(hostPort, { id: lastRequestId(hostPort), ok: true, result: { items: [] } });
      await vi.advanceTimersByTimeAsync(3_000);
      expect(progressRequests()).toHaveLength(3);
      watch.dispose();
    });

    interface WatchedAction {
      status: string;
      action: unknown;
    }
    const ITEM = { id: "a1", status: "done", reason: null, summary: "Done", sessionId: null };

    it.each([
      [
        "watch(listener, { actionIds })",
        (c: OmnigentClient, l: (snapshot: WatchedAction) => void) =>
          c.agent.watch(l, { actionIds: ["a1"] }),
      ],
      [
        "watch(actionId, listener)",
        (c: OmnigentClient, l: (snapshot: WatchedAction) => void) => c.agent.watch("a1", l),
      ],
      [
        "watch(listener, actionId)",
        (c: OmnigentClient, l: (snapshot: WatchedAction) => void) => c.agent.watch(l, "a1"),
      ],
      [
        "watch(receipt, listener)",
        (c: OmnigentClient, l: (snapshot: WatchedAction) => void) =>
          c.agent.watch({ actionId: "a1", delivery: "sent" } as OmnigentActionReceipt, l),
      ],
      [
        "watch(listener, { actionId })",
        (c: OmnigentClient, l: (snapshot: WatchedAction) => void) =>
          c.agent.watch(l, { actionId: "a1" }),
      ],
    ])("follows one action with %s, with that action on the snapshot", async (_form, start) => {
      const updates: WatchedAction[] = [];
      const watch = start(client, (snapshot) => updates.push(snapshot)) as { dispose: () => void };

      expect(lastFrameMessage(hostPort)).toMatchObject({
        method: "agent.v1.progress",
        params: { actionIds: ["a1"] },
      });
      respond(hostPort, { id: lastRequestId(hostPort), ok: true, result: { items: [ITEM] } });
      await vi.advanceTimersByTimeAsync(0);
      expect(updates.map((update) => update.status)).toEqual(["loading", "live"]);
      expect(updates[1].action).toEqual(ITEM);
      watch.dispose();
    });

    it("follows the action a receipt names through receipt.watch", async () => {
      const request = client.agent.request("run_card", {});
      respond(hostPort, {
        id: lastRequestId(hostPort),
        ok: true,
        result: { actionId: "a1", delivery: "sent" },
      });
      const receipt = await request;

      const watch = receipt.watch(() => undefined);

      expect(lastFrameMessage(hostPort)).toMatchObject({
        method: "agent.v1.progress",
        params: { actionIds: ["a1"] },
      });
      watch.dispose();
    });

    it("sends one key per logical request, and the caller's key on a retry", async () => {
      const sentKey = () =>
        (lastFrameMessage(hostPort) as { params: { operationId?: unknown } }).params.operationId;
      const first = client.agent.request("run_card", {});
      const firstKey = sentKey();
      respond(hostPort, { id: lastRequestId(hostPort), ok: true, result: { actionId: "a1" } });
      await first;
      const second = client.agent.request("run_card", {});
      const secondKey = sentKey();
      respond(hostPort, { id: lastRequestId(hostPort), ok: true, result: { actionId: "a2" } });
      await second;
      expect(typeof firstKey).toBe("string");
      expect(secondKey).not.toBe(firstKey);

      // A timed-out request names its key, so asking again with it is the same request.
      const timedOut = client.agent.request("run_card", {}, { timeoutMs: 100 });
      const key = sentKey();
      const failure = timedOut.catch((error: unknown) => error);
      await vi.advanceTimersByTimeAsync(150);
      expect(await failure).toMatchObject({ code: "DEADLINE_EXCEEDED", operationId: key });
      const retry = client.agent.request("run_card", {}, { idempotencyKey: String(key) });
      expect(sentKey()).toBe(key);
      respond(hostPort, { id: lastRequestId(hostPort), ok: true, result: { actionId: "a3" } });
      await retry;
    });

    it("explains the call and snapshot shape when watch is used without a listener", () => {
      expect(() => callUntyped(client.agent.watch, client.agent, ["a1"])).toThrow(
        /client\.agent\.watch\(function \(snapshot\).*actionIds.*items holds one/,
      );
      expect(() =>
        callUntyped(client.data.collection("cards").watch, client.data.collection("cards"), []),
      ).toThrow(/collection\.watch\(function \(snapshot\)/);
    });
  });
});

// The injected SDK runs against a real DOM; each case asserts the controls it changes.
describe("omnigent SDK permission guards", () => {
  const READ_ONLY_REASON = "You can view this gadget but not change it.";
  const ACTION_REASON = "Running actions needs Can edit access to this gadget.";

  // A fresh document per case, so one runtime's listeners never see another case's page.
  function page(html: string) {
    const doc = document.implementation.createHTMLDocument("gadget");
    doc.body.innerHTML = html;
    const runtime = loadRuntime({ document: doc, MutationObserver });
    const byId = <T extends HTMLElement>(id: string) => doc.getElementById(id) as T;
    return { doc, runtime, byId };
  }

  async function observed() {
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  }

  const MARKED = [
    '<button id="add" data-omnigent-requires="write">Add</button>',
    '<input id="title" data-omnigent-requires="write" value="Sample">',
    '<input id="done" type="checkbox" data-omnigent-requires="write">',
    '<button id="run" data-omnigent-requires="action" title="Run it">Run</button>',
    '<div id="card" data-omnigent-requires="write" draggable="true" contenteditable="true">Card</div>',
    '<button id="open">Open</button>',
  ].join("");

  it("disables marked controls for a read-only viewer and shows why", async () => {
    const { runtime, byId } = page(MARKED);
    const { client } = await connected(runtime, {
      ...CONTEXT,
      permissions: { writeData: false, publish: false },
    });

    expect(client.can("write")).toBe(false);
    expect(client.can("action")).toBe(false);
    expect(client.ui.blockedReason("write")).toBe(READ_ONLY_REASON);
    const add = byId<HTMLButtonElement>("add");
    expect(add.disabled).toBe(true);
    expect(add.getAttribute("aria-disabled")).toBe("true");
    expect(add.title).toBe(READ_ONLY_REASON);
    // A text field stays selectable, so the viewer can still quote it.
    const title = byId<HTMLInputElement>("title");
    expect(title.readOnly).toBe(true);
    expect(title.disabled).toBe(false);
    expect(byId<HTMLInputElement>("done").disabled).toBe(true);
    expect(byId("run").title).toBe(ACTION_REASON);
    const card = byId("card");
    expect(card.getAttribute("contenteditable")).toBe("false");
    expect(card.getAttribute("draggable")).toBe("false");
    expect(byId<HTMLButtonElement>("open").disabled).toBe(false);
    expect(byId("open").hasAttribute("data-omnigent-blocked")).toBe(false);

    const clicked = vi.fn();
    card.addEventListener("click", clicked);
    card.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(clicked).not.toHaveBeenCalled();
  });

  it("reports an empty selection once, however many clicks select nothing", async () => {
    const { doc, runtime } = page("<p>Nothing to select</p>");
    const { hostPort } = await connected(runtime);
    const selections = () =>
      (hostPort.peer?.posted ?? []).filter(
        (message) => (message as { method?: string }).method === "ui.v1.selection",
      ).length;

    // Each click's report runs before the next click, as with a person clicking.
    doc.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    await observed();
    doc.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    await observed();
    doc.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    await observed();

    expect(selections()).toBe(1);
  });

  it("keeps controls blocked when the page re-enables them or adds new ones", async () => {
    const { doc, runtime, byId } = page(MARKED);
    await connected(runtime, { ...CONTEXT, permissions: { writeData: false, publish: false } });

    byId<HTMLButtonElement>("add").disabled = false;
    const added = doc.createElement("button");
    added.setAttribute("data-omnigent-requires", "action");
    doc.body.appendChild(added);
    await observed();

    expect(byId<HTMLButtonElement>("add").disabled).toBe(true);
    expect(added.disabled).toBe(true);
    expect(added.title).toBe(ACTION_REASON);
  });

  it("blocks only what the viewer lacks", async () => {
    const { runtime, byId } = page(MARKED);
    const { client } = await connected(runtime, {
      ...CONTEXT,
      permissions: { writeData: true, publish: false },
    });

    expect(client.can("write")).toBe(true);
    expect(client.ui.blockedReason("write")).toBeNull();
    expect(byId<HTMLButtonElement>("add").disabled).toBe(false);
    expect(byId<HTMLInputElement>("title").readOnly).toBe(false);
    expect(byId<HTMLButtonElement>("run").disabled).toBe(true);
  });

  it("treats a revision without data.write as unable to change records", async () => {
    const { runtime, byId } = page(MARKED);
    const { client } = await connected(runtime, {
      ...CONTEXT,
      capabilities: ["data.read"],
      permissions: { writeData: true, publish: true },
    });

    expect(client.ui.blockedReason("write")).toBe("This gadget cannot change its records.");
    expect(byId<HTMLButtonElement>("add").disabled).toBe(true);
    expect(client.can("action")).toBe(true);
    expect(byId<HTMLButtonElement>("run").disabled).toBe(false);
    expect(byId("run").title).toBe("Run it");
  });
});

// The bootstrap builder's output string is the observable.
describe("omnigent SDK while the gadget session starts", () => {
  function page(html: string) {
    const doc = document.implementation.createHTMLDocument("gadget");
    doc.body.innerHTML = html;
    const runtime = loadRuntime({ document: doc, MutationObserver });
    return { doc, runtime };
  }

  // The stand-in port delivers on the next microtask, as a real one does asynchronously.
  async function delivered() {
    await Promise.resolve();
    await Promise.resolve();
  }

  it("says the session is starting, holds clicks on action controls and lets go after", async () => {
    const { doc, runtime } = page(
      '<button id="run" data-omnigent-requires="action">Run</button><button id="other">Other</button>',
    );
    const { hostPort, client } = await connected(runtime, {
      ...CONTEXT,
      permissions: { writeData: true, publish: true },
    });
    const changes: boolean[] = [];
    const stop = client.agent.onStarting((starting) => changes.push(starting));
    const clicks: string[] = [];
    for (const id of ["run", "other"]) {
      doc.getElementById(id)?.addEventListener("click", () => clicks.push(id));
    }

    expect(client.agent.starting()).toBe(false);
    hostPort.postMessage({ v: 1, kind: "event", event: "agent.starting", starting: true });
    await delivered();
    expect(client.agent.starting()).toBe(true);
    expect(doc.documentElement.hasAttribute("data-omnigent-session-starting")).toBe(true);
    doc.getElementById("run")?.click();
    doc.getElementById("other")?.click();
    // Only the control that runs an action waits; the page's own disabled state is untouched.
    expect(clicks).toEqual(["other"]);
    expect((doc.getElementById("run") as HTMLButtonElement).disabled).toBe(false);

    hostPort.postMessage({ v: 1, kind: "event", event: "agent.starting", starting: false });
    await delivered();
    expect(client.agent.starting()).toBe(false);
    expect(doc.documentElement.hasAttribute("data-omnigent-session-starting")).toBe(false);
    doc.getElementById("run")?.click();
    expect(clicks).toEqual(["other", "run"]);
    expect(changes).toEqual([true, false]);

    stop();
    hostPort.postMessage({ v: 1, kind: "event", event: "agent.starting", starting: true });
    await delivered();
    expect(changes).toEqual([true, false]);
  });

  it("ignores a malformed starting event", async () => {
    const { runtime } = page("");
    const { hostPort, client } = await connected(runtime);
    hostPort.postMessage({ v: 1, kind: "event", event: "agent.starting", starting: "yes" });
    hostPort.postMessage({ v: 1, kind: "event", event: "other", starting: true });
    await delivered();
    expect(client.agent.starting()).toBe(false);
  });
});

describe("buildOmnigentSdkBootstrapScript", () => {
  it("escapes markup in its configuration so it cannot close the script element", () => {
    const script = buildOmnigentSdkBootstrapScript({ frameId: "</script><img>", nonce: NONCE });

    expect(script).not.toContain("</script>");
    expect(script).toContain("\\u003c/script>\\u003cimg>");
  });
});
