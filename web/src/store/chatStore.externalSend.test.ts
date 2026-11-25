// `send` for a message that isn't the composer's (the Browser design tools' outbox).
import type * as IdentityModule from "@/lib/identity";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { conversationRegistry } from "./conversationRegistry";
import {
  handleSessionEvent,
  initChatStore,
  onSendDelivered,
  SESSION_READ_TIMEOUT_MS,
  useChatStore,
} from "./chatStore";
import { peekUnsentSend } from "@/lib/unsentSends";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentAuthorId: () => "user_a",
  getCurrentUserId: () => "user_a",
}));

const SESSION = "conv_existing";
const EVENTS = `/v1/sessions/${SESSION}/events`;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>();

/** The events POST answers with `post`; everything else, an empty idle session. */
function answerPost(post: () => Promise<Response>) {
  fetchMock.mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.endsWith(EVENTS) && init?.method === "POST") return post();
    if (url.includes("/stream")) return new Promise<Response>(() => {});
    return jsonResponse({ id: SESSION, status: "idle", items: [], data: [], has_more: false });
  });
}

const refusal = (code: string, status: number) => () =>
  Promise.resolve(jsonResponse({ error: { message: code, code } }, status));

const consumed = (itemId: string, clearedPendingId?: string) =>
  handleSessionEvent({
    type: "session_input_consumed",
    itemId,
    itemType: "message",
    ...(clearedPendingId ? { clearedPendingId } : {}),
    data: { role: "user", content: [{ type: "input_text", text: "design request" }] },
  });

function sendExternal(stableId: string, opts: Record<string, unknown> = {}) {
  return useChatStore.getState().send("design request", "agent_xyz", undefined, {
    external: true,
    stableId,
    ...opts,
  });
}

beforeEach(() => {
  initChatStore(new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  useChatStore.setState({
    conversationId: SESSION,
    abortController: new AbortController(),
    blocks: [],
    pendingUserMessages: [],
    queuedMessages: [],
    status: "idle",
    sessionStatus: "idle",
  });
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  useChatStore.getState().abortController?.abort();
  conversationRegistry.clear();
  vi.unstubAllGlobals();
});

describe("an external send", () => {
  it("reports no failure when the runner took it although its POST failed", async () => {
    let failPost = () => {};
    answerPost(
      () =>
        new Promise<Response>((resolve) => {
          failPost = () => resolve(jsonResponse({ error: "boom" }, 500));
        }),
    );
    const onError = vi.fn();
    const sending = sendExternal("d".repeat(32), { onError });
    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith(EVENTS))).toBe(true),
    );
    consumed("d".repeat(32));
    failPost();
    await sending;
    expect(onError).not.toHaveBeenCalled();
  });

  it("says whether its failure was the server refusing it", async () => {
    const onError = vi.fn();
    answerPost(refusal("host_offline", 503));
    await sendExternal("b".repeat(32), { onError });
    expect(onError).toHaveBeenCalledWith(expect.any(String), true);
    // A lost runner answers with a code too, but may have taken it first.
    onError.mockClear();
    answerPost(refusal("runner_unavailable", 503));
    await sendExternal("f".repeat(32), { onError });
    expect(onError).toHaveBeenCalledWith(expect.any(String), false);
    onError.mockClear();
    answerPost(() => Promise.reject(new TypeError("Failed to fetch")));
    await sendExternal("c".repeat(32), { onError });
    expect(onError).toHaveBeenCalledWith(expect.any(String), false);
  });

  it("isn't proven refused by the earlier refused attempt it retries", async () => {
    answerPost(() => Promise.reject(new TypeError("Failed to fetch")));
    const onError = vi.fn();
    await sendExternal("a".repeat(32), { onError, retriesRefused: true });
    expect(onError).toHaveBeenCalledWith(expect.any(String), false);
  });

  it("fails for an offline host instead of joining the composer's queue", async () => {
    answerPost(refusal("host_offline", 409));
    const onError = vi.fn();
    await sendExternal("e".repeat(32), { onError });
    expect(onError).toHaveBeenCalledTimes(1);
    expect(useChatStore.getState().queuedMessages).toEqual([]);
  });

  it("leaves the composer's retry id, restored send and reload recovery alone", async () => {
    const restored = {
      conversationId: SESSION,
      text: "failed composer text",
      files: [],
      stableId: "r".repeat(32),
      delivered: false,
    };
    let answer = () => {};
    answerPost(
      () =>
        new Promise<Response>((resolve) => {
          answer = () => resolve(jsonResponse({ queued: true, item_id: "ci_mock" }));
        }),
    );
    useChatStore.setState({ conversationId: null });
    await useChatStore.getState().switchTo(SESSION);
    useChatStore.setState({ pendingRetryStableId: restored.stableId, restoredSendDraft: restored });
    const sending = sendExternal("9".repeat(32));
    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith(EVENTS))).toBe(true),
    );
    expect(peekUnsentSend(SESSION)).toBeNull();
    answer();
    await sending;
    expect(useChatStore.getState().pendingRetryStableId).toBe(restored.stableId);
    expect(useChatStore.getState().restoredSendDraft).toEqual(restored);
  });

  it("tells the caller when policy denies it", async () => {
    answerPost(() => Promise.resolve(jsonResponse({ denied: true, reason: "not allowed" })));
    const onDenied = vi.fn();
    await sendExternal("8".repeat(32), { onDenied });
    expect(onDenied).toHaveBeenCalledTimes(1);
  });

  it("fails without posting once its signal is aborted", async () => {
    answerPost(() => Promise.resolve(jsonResponse({ queued: true, item_id: "ci_mock" })));
    const controller = new AbortController();
    controller.abort();
    const onError = vi.fn();
    await sendExternal("7".repeat(32), { onError, signal: controller.signal });
    expect(onError).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith(EVENTS))).toBe(false);
  });
});

describe("an external send that stops short", () => {
  const FILES = `/v1/sessions/${SESSION}/resources/files`;
  const posted = () =>
    fetchMock.mock.calls.some(
      ([url, init]) => String(url).endsWith(EVENTS) && init?.method === "POST",
    );

  /** Route like `answerPost`, with the screenshot upload answered by `upload`. */
  function answerUpload(upload: () => Promise<Response>, post: () => Promise<Response>) {
    answerPost(post);
    const routed = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input, init) =>
      String(input).endsWith(FILES) ? upload() : routed(input, init),
    );
  }
  const shot = () => [new File(["png"], "element.png", { type: "image/png" })];

  it("stops waiting on an unanswered POST once aborted, releasing the session", async () => {
    answerPost(() => new Promise<Response>(() => {}));
    const controller = new AbortController();
    const onError = vi.fn();
    const sending = sendExternal("1".repeat(32), { onError, signal: controller.signal });
    await vi.waitFor(() => expect(posted()).toBe(true));
    controller.abort();
    // Settles although the POST never answers.
    await sending;
    // It may have arrived: not refused.
    expect(onError).toHaveBeenCalledWith(expect.any(String), false);
    expect(useChatStore.getState().status).toBe("idle");
    expect(useChatStore.getState().pendingUserMessages).toEqual([]);
  });

  it("stops waiting on a stalled upload once aborted, without posting", async () => {
    answerUpload(
      () => new Promise<Response>(() => {}),
      () => Promise.resolve(jsonResponse({ queued: true, item_id: "ci_mock" })),
    );
    const controller = new AbortController();
    const onError = vi.fn();
    const sending = useChatStore.getState().send("design request", "agent_xyz", shot(), {
      external: true,
      stableId: "2".repeat(32),
      onError,
      signal: controller.signal,
    });
    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith(FILES))).toBe(true),
    );
    controller.abort();
    await sending;
    expect(onError).toHaveBeenCalledWith(expect.any(String), true);
    expect(posted()).toBe(false);
    expect(useChatStore.getState().status).toBe("idle");
  });

  it("asks its caller once the upload is done, and posts only if nothing holds it back", async () => {
    let finishUpload = () => {};
    answerUpload(
      () =>
        new Promise<Response>((resolve) => {
          finishUpload = () => resolve(jsonResponse({ id: "file_1", filename: "element.png" }));
        }),
      () => Promise.resolve(jsonResponse({ queued: true, item_id: "ci_mock" })),
    );
    const beforePost = vi.fn(async (): Promise<string | null> => "Another account is signed in");
    const onError = vi.fn();
    const uploads = () => fetchMock.mock.calls.filter(([url]) => String(url).endsWith(FILES));
    const posts = () =>
      fetchMock.mock.calls.filter(
        ([url, init]) => String(url).endsWith(EVENTS) && init?.method === "POST",
      ).length;
    // The same Files on the refusal and on the retry: the owner's retry reuses their upload.
    const files = shot();
    const send = () =>
      useChatStore.getState().send("design request", "agent_xyz", files, {
        external: true,
        stableId: "6".repeat(32),
        onError,
        beforePost,
      });
    const first = send();
    await vi.waitFor(() => expect(uploads().length).toBe(1));
    expect(beforePost).not.toHaveBeenCalled();
    finishUpload();
    await first;
    expect(beforePost).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledWith("Another account is signed in", true);
    expect(posts()).toBe(0);
    expect(useChatStore.getState().status).toBe("idle");
    // Once nothing holds it back, the same request posts, with no second upload.
    beforePost.mockResolvedValue(null);
    onError.mockClear();
    await send();
    expect(onError).not.toHaveBeenCalled();
    expect(uploads().length).toBe(1);
    expect(posts()).toBe(1);
  });

  it.each(["held back", "its upload fails", "its upload is stopped"] as const)(
    "leaves a turn that started during its upload running when %s",
    async (how) => {
      let finishUpload = () => {};
      let failUpload = () => {};
      answerUpload(
        () =>
          new Promise<Response>((resolve, reject) => {
            finishUpload = () => resolve(jsonResponse({ id: "file_1", filename: "element.png" }));
            failUpload = () => reject(new TypeError("Failed to fetch"));
          }),
        () => Promise.resolve(jsonResponse({ queued: true, item_id: "ci_mock" })),
      );
      const controller = new AbortController();
      const onError = vi.fn();
      const sending = useChatStore.getState().send("design request", "agent_xyz", shot(), {
        external: true,
        stableId: "7".repeat(32),
        onError,
        signal: controller.signal,
        beforePost: async () => "A turn started while it was uploading",
      });
      await vi.waitFor(() =>
        expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith(FILES))).toBe(true),
      );
      // Another client's turn starts while the screenshot uploads (as the stream writes it).
      const entry = conversationRegistry.peek(SESSION)!;
      const other = { responseId: "response_other", state: "streaming" as const, error: null };
      entry.setState({ sessionStatus: "running", activeResponse: other, backgroundTaskCount: 1 });
      if (how === "held back") finishUpload();
      else if (how === "its upload fails") failUpload();
      else controller.abort();
      await sending;
      expect(onError).toHaveBeenCalledWith(expect.any(String), true);
      expect(posted()).toBe(false);
      const state = entry.getState();
      expect(state.activeResponse).toEqual(other);
      expect(state.sessionStatus).toBe("running");
      expect(state.backgroundTaskCount).toBe(1);
      expect(state.pendingUserMessages).toEqual([]);
      expect(state.blocks).toEqual([]);
    },
  );

  it("is refused when its upload fails before the POST", async () => {
    answerUpload(
      () => Promise.reject(new TypeError("Failed to fetch")),
      () => Promise.resolve(jsonResponse({ queued: true, item_id: "ci_mock" })),
    );
    const onError = vi.fn();
    await useChatStore.getState().send("design request", "agent_xyz", shot(), {
      external: true,
      stableId: "3".repeat(32),
      onError,
    });
    expect(onError).toHaveBeenCalledWith(expect.any(String), true);
    expect(posted()).toBe(false);
  });

  it("is refused by a coded 503 the server recorded as the runner rejecting it", async () => {
    const stableId = "4".repeat(32);
    answerPost(refusal("runner_unavailable", 503));
    const routed = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input, init) =>
      String(input).split("?")[0] === `/v1/sessions/${SESSION}` && (init?.method ?? "GET") === "GET"
        ? Promise.resolve(
            jsonResponse({
              id: SESSION,
              status: "failed",
              items: [],
              last_task_error: { code: "runner_rejected_event", message: "no", item_id: stableId },
            }),
          )
        : routed(input, init),
    );
    const onError = vi.fn();
    await sendExternal(stableId, { onError });
    expect(onError).toHaveBeenCalledWith(expect.any(String), true);
  });

  it("tells the caller when a POST it stopped waiting on is accepted after all", async () => {
    let answer = () => {};
    answerPost(
      () =>
        new Promise<Response>((resolve) => {
          answer = () => resolve(jsonResponse({ queued: true, item_id: "ci_mock" }));
        }),
    );
    const controller = new AbortController();
    const onError = vi.fn();
    const onAcceptedLate = vi.fn();
    const sending = sendExternal("5".repeat(32), {
      onError,
      onAcceptedLate,
      signal: controller.signal,
    });
    await vi.waitFor(() => expect(posted()).toBe(true));
    controller.abort();
    await sending;
    expect(onError).toHaveBeenCalledWith(expect.any(String), false);
    expect(onAcceptedLate).not.toHaveBeenCalled();
    answer();
    await vi.waitFor(() => expect(onAcceptedLate).toHaveBeenCalledTimes(1));
  });

  /** A coded 503 whose refusal check reads a session snapshot that never answers. */
  function refuseWithStalledSnapshot() {
    answerPost(refusal("runner_unavailable", 503));
    const routed = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input, init) =>
      posted() && isSnapshotRead(input, init)
        ? new Promise<Response>(() => {})
        : routed(input, init),
    );
  }
  const isSnapshotRead = (input: RequestInfo | URL, init?: RequestInit) =>
    String(input).split("?")[0] === `/v1/sessions/${SESSION}` && (init?.method ?? "GET") === "GET";
  // A read after the POST: the refusal check's.
  const snapshotRead = () => {
    const calls = fetchMock.mock.calls;
    const post = calls.findIndex(
      ([url, init]) => String(url).endsWith(EVENTS) && init?.method === "POST",
    );
    return post >= 0 && calls.slice(post).some(([url, init]) => isSnapshotRead(url, init));
  };

  it("stops waiting on its refusal check once aborted", async () => {
    refuseWithStalledSnapshot();
    const controller = new AbortController();
    const onError = vi.fn();
    const sending = sendExternal("6".repeat(32), { onError, signal: controller.signal });
    await vi.waitFor(() => expect(snapshotRead()).toBe(true));
    controller.abort();
    await sending;
    // Not shown refused: delivery stays unknown.
    expect(onError).toHaveBeenCalledWith(expect.any(String), false);
  });

  it("gives up on a refusal check the server never answers", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      refuseWithStalledSnapshot();
      const onError = vi.fn();
      const sending = sendExternal("0".repeat(32), { onError });
      await vi.waitFor(() => expect(snapshotRead()).toBe(true));
      await vi.advanceTimersByTimeAsync(SESSION_READ_TIMEOUT_MS);
      await sending;
      expect(onError).toHaveBeenCalledWith(expect.any(String), false);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("onSendDelivered", () => {
  it("hears each send the runner takes, by its item and by the pending input it drained", () => {
    const heard = vi.fn();
    const stop = onSendDelivered(heard);
    try {
      consumed("g".repeat(32));
      expect(heard).toHaveBeenCalledWith("g".repeat(32));
      consumed("mirror_item", "pending_abc");
      expect(heard).toHaveBeenCalledWith("mirror_item");
      expect(heard).toHaveBeenCalledWith("pending_abc");
    } finally {
      stop();
    }
    consumed("h".repeat(32));
    expect(heard).not.toHaveBeenCalledWith("h".repeat(32));
  });
});
