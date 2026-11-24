import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { initChatStore, useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { setOmnigentHostConfig } from "./host";
import { writeAlwaysSteer } from "./alwaysSteerPreferences";
import { setSessionHostResolver } from "./identity";
import { requestGoogleDocReview } from "./googleDocsRequests";

const URL = "https://docs.google.com/document/u/2/d/example/edit?usp=sharing#tab=t.one";
const fetcher = vi.fn<typeof fetch>();
let sessionId: string;
let sequence = 0;
let current: boolean;
let client: QueryClient;
const posts = () =>
  fetcher.mock.calls.filter(
    ([url, init]) => String(url).endsWith("/events") && init?.method === "POST",
  );
const request = (url = URL, permission: number | null = 2) =>
  requestGoogleDocReview(sessionId, url, permission, () => current);
const idle = () =>
  useChatStore.setState({ status: "idle", sessionStatus: "idle", pendingUserMessages: [] });
beforeEach(() => {
  sessionId = `review_${++sequence}`;
  current = true;
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  initChatStore(client);
  useChatStore.setState({
    conversationId: sessionId,
    boundAgentId: "agent",
    abortController: new AbortController(),
    blocks: [],
    pendingUserMessages: [],
    queuedMessages: [],
    status: "idle",
    sessionStatus: "idle",
    startupPending: false,
  });
  setOmnigentHostConfig({ serverIdentity: "backend-a", fetcher });
  setSessionHostResolver(null);
  fetcher
    .mockReset()
    .mockImplementation(async (_url, init) =>
      init?.method === "POST"
        ? Response.json({ queued: true, item_id: "review_input" })
        : Response.json({ id: sessionId, status: "idle", items: [], data: [], has_more: false }),
    );
  vi.stubGlobal("fetch", fetcher);
  writeAlwaysSteer(false);
});
afterEach(() => {
  useChatStore.getState().abortController?.abort();
  for (const entry of conversationRegistry.all()) entry.getState().abortController?.abort();
  conversationRegistry.clear();
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("posts the captured original URL through normal chat delivery with native comments and read-only fallback", async () => {
  await request();
  expect(posts()).toHaveLength(1);
  const [url, init] = posts()[0];
  expect(url).toBe(`/v1/sessions/${sessionId}/events`);
  const event = JSON.parse(String(init?.body));
  expect(event.data.stable_id).toMatch(/^[a-f0-9]{32}$/);
  const text = event.data.content[0].text;
  expect(text).toContain(URL);
  expect(text).toContain("native comment threads where permitted");
  expect(text).toContain("read-only or commenting is unavailable");
  expect(text).toContain("findings and suggested changes in chat");
  expect(text).toContain("Report missing Google access in chat");
  expect(fetcher.mock.calls.every(([input]) => String(input).startsWith("/v1/"))).toBe(true);
});

it.each([1, 0])(
  "rejects a session reader without sending or queueing (permission=%s)",
  async (permission) => {
    await expect(request(URL, permission)).rejects.toThrow("edit access");
    expect(fetcher).not.toHaveBeenCalled();
    expect(useChatStore.getState().queuedMessages).toEqual([]);
  },
);

it("rejects stale activations, unsupported targets, and unconnected sessions", async () => {
  current = false;
  await expect(request()).rejects.toThrow("previous session");
  current = true;
  await expect(request(URL + "&heading=h.example")).rejects.toThrow("supported");
  useChatStore.setState({ boundAgentId: null });
  await expect(request()).rejects.toThrow("connect");
  expect(posts()).toEqual([]);
});

it("keeps a busy review in the normal queue, deduplicates it, and flushes its captured URL once", async () => {
  useChatStore.setState({ status: "streaming", sessionStatus: "running" });
  await request();
  await request();
  expect(posts()).toEqual([]);
  expect(useChatStore.getState().queuedMessages).toHaveLength(1);
  const stableId = useChatStore.getState().queuedMessages[0].stableId;
  idle();
  useChatStore.getState().maybeFlushQueuedHead();
  await vi.waitFor(() => expect(posts()).toHaveLength(1));
  expect(JSON.parse(String(posts()[0][1]?.body)).data).toMatchObject({
    stable_id: stableId,
    content: [{ type: "input_text", text: expect.stringContaining(URL) }],
  });
  await vi.waitFor(() => expect(useChatStore.getState().queuedMessages).toEqual([]));
});

it("deduplicates concurrent and already pending reviews", async () => {
  let finish!: (value: Response) => void;
  fetcher.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const sending = request();
  await vi.waitFor(() => expect(posts()).toHaveLength(1));
  await request();
  expect(posts()).toHaveLength(1);
  finish(Response.json({ queued: true, item_id: "pending_review" }));
  await sending;
  await request();
  expect(posts()).toHaveLength(1);
});

it("honors always-steer while preserving an earlier queued message", async () => {
  writeAlwaysSteer(true);
  useChatStore.setState({ status: "streaming", sessionStatus: "running" });
  await request();
  expect(posts()).toHaveLength(1);
  idle();
  useChatStore.setState({
    status: "streaming",
    sessionStatus: "running",
    queuedMessages: [{ queueId: "earlier", conversationId: sessionId, text: "Earlier input" }],
  });
  await request(URL.replace("example", "other"));
  expect(posts()).toHaveLength(1);
  expect(useChatStore.getState().queuedMessages.map((message) => message.text)).toEqual([
    "Earlier input",
    expect.stringContaining("/d/other/"),
  ]);
});

it.each(["activation", "backend", "transport"])(
  "rejects a queued review from a departed %s without requeueing into the new scope",
  async (change) => {
    useChatStore.setState({ status: "streaming", sessionStatus: "running" });
    await request();
    if (change === "activation") current = false;
    else if (change === "backend") setOmnigentHostConfig({ serverIdentity: "backend-b", fetcher });
    else setOmnigentHostConfig({ serverIdentity: "backend-a", fetcher: vi.fn() });
    idle();
    useChatStore.getState().maybeFlushQueuedHead();
    await vi.waitFor(() => expect(useChatStore.getState().queuedMessages).toEqual([]));
    expect(posts()).toEqual([]);
  },
);

it("reports immediate policy denial and queued denial in normal chat error handling", async () => {
  fetcher.mockImplementation(async () =>
    Response.json({ denied: true, reason: "Blocked by policy" }),
  );
  await expect(request()).rejects.toThrow("policy blocked");
  idle();
  useChatStore.setState({ status: "streaming", sessionStatus: "running" });
  await request();
  idle();
  useChatStore.getState().maybeFlushQueuedHead();
  await vi.waitFor(() =>
    expect(useChatStore.getState().blocks).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          type: "error",
          message: expect.stringContaining("policy blocked"),
        }),
      ]),
    ),
  );
  expect(useChatStore.getState().queuedMessages).toEqual([]);
});

it.each(["denied", "refused", "stale"])(
  "reports a startup shortcut review's %s outcome without duplicate delivery",
  async (outcome) => {
    useChatStore.setState({ startupPending: true, status: "streaming", sessionStatus: "running" });
    let finish!: (response: Response) => void;
    fetcher.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await request();
    await request();
    await vi.waitFor(() => expect(posts()).toHaveLength(1));
    const stableId = JSON.parse(String(posts()[0][1]?.body)).data.stable_id;
    if (outcome === "stale") current = false;
    finish(
      outcome === "denied"
        ? Response.json({ denied: true, reason: "Blocked by policy" })
        : Response.json(
            { error: { code: "host_offline", message: "Host unavailable" } },
            { status: 409 },
          ),
    );
    await vi.waitFor(() => {
      expect(useChatStore.getState().pendingUserMessages).toEqual([]);
      if (outcome !== "stale")
        expect(useChatStore.getState().blocks).toEqual(
          expect.arrayContaining([
            expect.objectContaining({
              type: "error",
              message: expect.stringContaining(
                outcome === "denied" ? "policy blocked" : "Host unavailable",
              ),
            }),
          ]),
        );
    });
    if (outcome === "refused")
      expect(useChatStore.getState().queuedMessages).toMatchObject([
        { stableId, requiresRetry: true, text: expect.stringContaining(URL) },
      ]);
    else expect(useChatStore.getState().queuedMessages).toEqual([]);
    if (outcome === "stale") expect(useChatStore.getState().blocks).toEqual([]);
    useChatStore.setState({ startupPending: false, status: "idle", sessionStatus: "idle" });
    useChatStore.getState().maybeFlushQueuedHead();
    expect(posts()).toHaveLength(1);
  },
);

it("retains a failed queued request and its stable ID for the queue's explicit retry", async () => {
  useChatStore.setState({ status: "streaming", sessionStatus: "running" });
  await request();
  const stableId = useChatStore.getState().queuedMessages[0].stableId;
  fetcher.mockImplementation(async () =>
    Response.json(
      { error: { code: "host_offline", message: "Host unavailable" } },
      { status: 409 },
    ),
  );
  idle();
  useChatStore.getState().maybeFlushQueuedHead();
  await vi.waitFor(() =>
    expect(useChatStore.getState().queuedMessages).toMatchObject([
      { stableId, requiresRetry: true },
    ]),
  );
  expect(useChatStore.getState().blocks).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        type: "error",
        message: expect.stringContaining("Host unavailable"),
      }),
    ]),
  );
});

it("pins review delivery to the originating live grid session", async () => {
  const id = sessionId + "_grid";
  conversationRegistry.acquire(id).setState({
    boundAgentId: "grid-agent",
    status: "idle",
    sessionStatus: "idle",
    startupPending: false,
    abortController: new AbortController(),
  });
  await requestGoogleDocReview(id, URL, 2, () => true);
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe(`/v1/sessions/${id}/events`);
  expect(useChatStore.getState().conversationId).toBe(sessionId);
});

it.each([true, false])(
  "preserves the review guard through the background queue (current=%s)",
  async (active) => {
    useChatStore.setState({ status: "streaming", sessionStatus: "running" });
    await request();
    client.setQueryData(["conversations"], {
      pages: [{ data: [{ id: sessionId, status: "idle" }] }],
      pageParams: [null],
    });
    useChatStore.setState({
      conversationId: sessionId + "_other",
      status: "idle",
      sessionStatus: "idle",
    });
    current = active;
    useChatStore.getState().flushBackgroundQueues();
    await vi.waitFor(() => expect(useChatStore.getState().queuedMessages).toEqual([]));
    if (active) await vi.waitFor(() => expect(posts()).toHaveLength(1));
    else {
      // Wait for the real queued send's rejection/cleanup, beyond dequeueing.
      await vi.waitFor(() =>
        expect(conversationRegistry.peek(sessionId)?.getState().pendingUserMessages).toEqual([]),
      );
      expect(posts()).toEqual([]);
    }
  },
);
