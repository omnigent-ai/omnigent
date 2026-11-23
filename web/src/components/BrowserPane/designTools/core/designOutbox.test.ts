import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { browserViewId } from "@/hooks/useBrowserTabs";
import { conversationRegistry } from "@/store/conversationRegistry";
import type * as ChatStore from "@/store/chatStore";
import type * as Identity from "@/lib/identity";
import { nativePendingId } from "./pendingInputId";
import { useChatStore, type SendOptions } from "@/store/chatStore";

// What the chat store tells the outbox: sends the runner took.
const chat = vi.hoisted(() => ({ delivered: new Set<(stableId: string) => void>() }));
vi.mock("@/store/chatStore", async (importOriginal) => ({
  ...(await importOriginal<typeof ChatStore>()),
  onSendDelivered: (listener: (stableId: string) => void) => {
    chat.delivered.add(listener);
    return () => chat.delivered.delete(listener);
  },
}));
// No account is known here; the server says none is signed in (no sign-in on this server).
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof Identity>()),
  fetchSignedInUserId: async () => null,
}));
const runnerTook = (id: string) => {
  for (const listener of chat.delivered) listener(id);
};

import {
  SLOW_SEND_MS,
  STALLED_SEND_MS,
  TURN_START_MS,
  designOutboxTracks,
  forget,
  queueDesignRequest,
  resetDesignOutbox,
  retryDesignRequest,
  useDesignOutbox,
} from "./designOutbox";

const initial = useChatStore.getState();
const SESSION = "conv_outbox";
const TAB = browserViewId(SESSION, "tab_1");

const send = vi.fn(
  async (_text: string, _agent: string, _files?: File[], _opts?: SendOptions): Promise<void> => {},
);

const flush = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });
const statusOf = (id: string) =>
  useDesignOutbox.getState().requests.find((request) => request.id === id)?.status;
const sentIds = () => send.mock.calls.map(([, , , opts]) => opts?.stableId);

function setSession(overrides: Partial<ReturnType<typeof useChatStore.getState>> = {}) {
  useChatStore.setState({
    conversationId: SESSION,
    boundAgentId: "agent_1",
    status: "idle",
    sessionStatus: "idle",
    startupPending: false,
    queuedMessages: [],
    pendingUserMessages: [],
    pendingModelChange: null,
    send,
    ...overrides,
  });
}

beforeEach(() => setSession());

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  resetDesignOutbox();
  send.mockReset();
  send.mockImplementation(async () => {});
  useChatStore.setState(initial, true);
});

describe("design outbox", () => {
  it("sends through the chat store under the request's id, apart from the composer", async () => {
    const shot = new File(["png"], "shot.png", { type: "image/png" });
    const id = queueDesignRequest({
      tabId: TAB,
      label: "Draw",
      text: "Make it bigger",
      files: [shot],
    });
    await flush();
    expect(send).toHaveBeenCalledWith(
      "Make it bigger",
      "agent_1",
      [shot],
      expect.objectContaining({ stableId: id, pinnedConversationId: SESSION, external: true }),
    );
    expect(statusOf(id)).toBe("sent");
  });

  it("waits for a running turn to end instead of steering it", async () => {
    setSession({ sessionStatus: "running" });
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Green" });
    await flush();
    expect(send).not.toHaveBeenCalled();
    expect(statusOf(id)).toBe("waiting");
    setSession({ sessionStatus: "idle" });
    await flush();
    expect(statusOf(id)).toBe("sent");
  });

  it("waits again when a turn starts elsewhere while its screenshot uploads", async () => {
    // The chat store asks once the upload is done, just before it would post.
    send.mockImplementationOnce(async (_text, _agent, _files, opts) => {
      setSession({ sessionStatus: "running" });
      const heldBack = await opts?.beforePost?.();
      if (heldBack) opts?.onError?.(heldBack, true);
    });
    const id = queueDesignRequest({ tabId: TAB, label: "Draw", text: "Green" });
    await flush();
    expect(send).toHaveBeenCalledOnce();
    expect(statusOf(id)).toBe("waiting");
    setSession({ sessionStatus: "idle" });
    await flush();
    expect(sentIds()).toEqual([id, id]);
    expect(statusOf(id)).toBe("sent");
  });

  it("waits behind the composer's queue, a model switch and a starting session", async () => {
    setSession({
      queuedMessages: [{ queueId: "q1", text: "first", conversationId: SESSION }] as never,
    });
    queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Green" });
    await flush();
    setSession({ queuedMessages: [], pendingModelChange: "sonnet" });
    await flush();
    setSession({ pendingModelChange: null, startupPending: true });
    await flush();
    expect(send).not.toHaveBeenCalled();
    setSession({ startupPending: false });
    await flush();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("sends one request per turn", async () => {
    const first = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "One" });
    const second = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Two" });
    await flush();
    expect(statusOf(first)).toBe("sent");
    expect(statusOf(second)).toBe("waiting");
    // The first one's turn starts and ends; then the second goes.
    setSession({ sessionStatus: "running" });
    await flush();
    setSession({ sessionStatus: "idle" });
    await flush();
    expect(statusOf(second)).toBe("sent");
    expect(sentIds()).toEqual([first, second]);
  });

  it("goes on when a sent request starts no turn", async () => {
    vi.useFakeTimers();
    queueDesignRequest({ tabId: TAB, label: "Annotate", text: "One" });
    const second = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Two" });
    await vi.advanceTimersByTimeAsync(0);
    expect(statusOf(second)).toBe("waiting");
    await vi.advanceTimersByTimeAsync(TURN_START_MS + 1000);
    expect(statusOf(second)).toBe("sent");
  });

  it("keeps a denied request, and a failed one to retry under the same id", async () => {
    send.mockImplementationOnce(async (_t, _a, _f, opts) => opts?.onDenied?.());
    const denied = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Denied" });
    await flush();
    expect(useDesignOutbox.getState().requests[0]).toMatchObject({
      status: "denied",
      error: expect.stringContaining("policy"),
    });
    retryDesignRequest(denied);
    expect(statusOf(denied)).toBe("denied");
    forget(denied);
    send.mockImplementationOnce(async (_t, _a, _f, opts) => opts?.onError?.("Host offline", true));
    const failed = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Retry me" });
    await flush();
    expect(useDesignOutbox.getState().requests[0]).toMatchObject({
      status: "failed",
      error: "Couldn't send: Host offline",
    });
    retryDesignRequest(failed);
    await flush();
    expect(statusOf(failed)).toBe("sent");
    expect(sentIds().slice(-2)).toEqual([failed, failed]);
  });

  it("says a slow send is slow, but never sends it again while it runs", async () => {
    vi.useFakeTimers();
    let answer = () => {};
    send.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          answer = resolve;
        }),
    );
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Slow" });
    await vi.advanceTimersByTimeAsync(SLOW_SEND_MS);
    expect(useDesignOutbox.getState().requests[0]).toMatchObject({ status: "sending", slow: true });
    retryDesignRequest(id);
    forget(id);
    await vi.advanceTimersByTimeAsync(TURN_START_MS * 2);
    expect(send).toHaveBeenCalledTimes(1);
    answer();
    await vi.advanceTimersByTimeAsync(0);
    expect(statusOf(id)).toBe("sent");
  });

  it("stops waiting on a send that never answers, so the session's next ones go", async () => {
    vi.useFakeTimers();
    let answer = () => {};
    send.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          answer = resolve;
        }),
    );
    const stuck = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Stuck" });
    const next = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Next" });
    await vi.advanceTimersByTimeAsync(STALLED_SEND_MS);
    expect(useDesignOutbox.getState().requests.find((r) => r.id === stuck)).toMatchObject({
      status: "failed",
      error: expect.stringContaining("may have reached the agent"),
    });
    // Its send is stopped, which settles it in the chat store and frees the session.
    expect(send.mock.calls[0]![3]?.signal?.aborted).toBe(true);
    // Its POST may still land and start a turn: the next waits as if it had been sent.
    expect(statusOf(next)).toBe("waiting");
    await vi.advanceTimersByTimeAsync(TURN_START_MS + 1);
    expect(statusOf(next)).toBe("sent");
    // Its POST answering at last is news that it arrived.
    answer();
    await vi.advanceTimersByTimeAsync(0);
    expect(statusOf(stuck)).toBe("sent");
    expect(sentIds()).toEqual([stuck, next]);
  });

  it("counts a failed send the runner turns out to have taken as sent, not to retry", async () => {
    send.mockImplementationOnce(async (_t, _a, _f, opts) => opts?.onError?.("Network down"));
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Arrived" });
    await flush();
    expect(statusOf(id)).toBe("failed");
    runnerTook(id);
    expect(statusOf(id)).toBe("sent");
    retryDesignRequest(id);
    await flush();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("says one that may have arrived may have, and sends it again only on the user's Retry", async () => {
    send.mockImplementationOnce(async (_t, _a, _f, opts) => opts?.onError?.("Network down"));
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Maybe" });
    await flush();
    expect(useDesignOutbox.getState().requests[0]).toMatchObject({
      status: "failed",
      delivery: "unknown",
      error: expect.stringContaining("Look in the chat"),
    });
    // Nothing sends it again on its own, however long it waits.
    setSession({ sessionStatus: "idle" });
    await flush();
    expect(send).toHaveBeenCalledTimes(1);
    retryDesignRequest(id);
    await flush();
    expect(sentIds()).toEqual([id, id]);
    expect(send.mock.calls[1]![3]).toMatchObject({ retriesRefused: false });
    expect(statusOf(id)).toBe("sent");
  });

  it("resends one the server refused as a refused one, whose old item proves nothing", async () => {
    send.mockImplementationOnce(async (_t, _a, _f, opts) => opts?.onError?.("Busy", true));
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Refused" });
    await flush();
    expect(useDesignOutbox.getState().requests[0]).toMatchObject({
      status: "failed",
      delivery: "refused",
      error: "Couldn't send: Busy",
    });
    retryDesignRequest(id);
    await flush();
    expect(send.mock.calls[1]![3]).toMatchObject({ stableId: id, retriesRefused: true });
  });

  it("stops a stalled attempt before a Retry sends it again, so only one posts", async () => {
    vi.useFakeTimers();
    const signals: (AbortSignal | undefined)[] = [];
    send.mockImplementationOnce(async (_t, _a, _f, opts) => {
      signals.push(opts?.signal);
      await new Promise(() => {});
    });
    send.mockImplementationOnce(async (_t, _a, _f, opts) => {
      signals.push(opts?.signal);
    });
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Uploading" });
    await vi.advanceTimersByTimeAsync(STALLED_SEND_MS);
    expect(statusOf(id)).toBe("failed");
    retryDesignRequest(id);
    await vi.advanceTimersByTimeAsync(TURN_START_MS + 1);
    expect(signals.map((signal) => signal?.aborted)).toEqual([true, false]);
    expect(statusOf(id)).toBe("sent");
  });

  it("hears a native session's agent take one by the pending input it drained", async () => {
    send.mockImplementationOnce(async (_t, _a, _f, opts) => opts?.onError?.("Network down"));
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Native" });
    await flush();
    expect(statusOf(id)).toBe("failed");
    runnerTook(await nativePendingId(SESSION, id));
    expect(statusOf(id)).toBe("sent");
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("keeps nothing of a request once it's dropped", async () => {
    send.mockImplementationOnce(async (_t, _a, _f, opts) => opts?.onError?.("Network down"));
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Dropped" });
    await flush();
    await nativePendingId(SESSION, id);
    await flush();
    expect(statusOf(id)).toBe("failed");
    expect(designOutboxTracks(id)).toBe(true);
    forget(id);
    expect(statusOf(id)).toBeUndefined();
    expect(designOutboxTracks(id)).toBe(false);
    // Removed, it's never sent again.
    await flush();
    expect(statusOf(id)).toBeUndefined();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("counts a send the runner took while it ran as sent, though its POST then failed", async () => {
    // Without crypto.subtle the pending id is worked out at once, before the send runs.
    vi.stubGlobal("crypto", {});
    send.mockImplementationOnce(async (_t, _a, _f, opts) => {
      runnerTook(await nativePendingId(SESSION, opts!.stableId!));
      opts?.onError?.("Network down");
    });
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Native" });
    await flush();
    expect(statusOf(id)).toBe("sent");
  });

  it("counts a send as sent when the runner's receipt beats working out its pending id", async () => {
    const { subtle } = globalThis.crypto;
    const digest = subtle.digest.bind(subtle);
    let hold = true;
    let release = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.spyOn(subtle, "digest").mockImplementation(async (...args) => {
      if (hold) await held;
      return digest(...args);
    });
    send.mockImplementationOnce(async (_t, _a, _f, opts) => {
      hold = false;
      runnerTook(await nativePendingId(SESSION, opts!.stableId!));
      opts?.onError?.("Network down");
    });
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Native" });
    await vi.waitFor(() => expect(statusOf(id)).toBe("failed"));
    // The outbox's own hash finishes last: the receipt it held now counts.
    release();
    await vi.waitFor(() => expect(statusOf(id)).toBe("sent"));
  });

  it("counts a stalled send the runner took while it ran as sent, leaving its turn running", async () => {
    // Without crypto.subtle the pending id is worked out at once, before the send runs.
    vi.stubGlobal("crypto", {});
    vi.useFakeTimers();
    let signal: AbortSignal | undefined;
    send.mockImplementationOnce(async (_t, _a, _f, opts) => {
      signal = opts?.signal;
      runnerTook(await nativePendingId(SESSION, opts!.stableId!));
      await new Promise(() => {});
    });
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Native" });
    await vi.advanceTimersByTimeAsync(STALLED_SEND_MS);
    expect(statusOf(id)).toBe("sent");
    // Not stopped, as stopping it would fail the turn it started; nor once it's dropped.
    expect(signal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(TURN_START_MS * 2);
    expect(statusOf(id)).toBeUndefined();
    expect(signal?.aborted).toBe(false);
  });

  it("counts a stalled send whose POST went through after all as sent", async () => {
    vi.useFakeTimers();
    let accepted = () => {};
    send.mockImplementationOnce(async (_t, _a, _f, opts) => {
      accepted = opts!.onAcceptedLate!;
      // Stopped, the chat store's send fails at once; its POST is still out.
      await new Promise<void>((resolve) => {
        opts?.signal?.addEventListener("abort", () => resolve());
      });
      opts?.onError?.("Aborted");
    });
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Late" });
    await vi.advanceTimersByTimeAsync(STALLED_SEND_MS);
    expect(statusOf(id)).toBe("failed");
    accepted();
    expect(statusOf(id)).toBe("sent");
  });

  it("asks before the app unloads while any request isn't sent, failed ones too", async () => {
    const unload = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(unload()).toBe(false);
    send.mockImplementationOnce(async (_t, _a, _f, opts) => opts?.onError?.("offline"));
    const id = queueDesignRequest({ tabId: TAB, label: "Draw", text: "Note" });
    await flush();
    expect(statusOf(id)).toBe("failed");
    expect(unload()).toBe(true);
    forget(id);
    expect(unload()).toBe(false);
  });

  it("waits for a session this tab hasn't loaded, and goes once it's ready, without polling", async () => {
    const timers = vi.spyOn(globalThis, "setTimeout");
    const other = browserViewId("conv_elsewhere", "tab_1");
    const id = queueDesignRequest({ tabId: other, label: "Annotate", text: "Later" });
    await flush();
    expect(send).not.toHaveBeenCalled();
    expect(statusOf(id)).toBe("waiting");
    // Nothing re-checks on a timer while it waits.
    expect(timers.mock.calls.filter(([, ms]) => (ms ?? 0) > 0)).toEqual([]);
    timers.mockRestore();
    // The session loads in the background and goes idle: the registry's change wakes it.
    const entry = conversationRegistry.acquire("conv_elsewhere");
    try {
      entry.setState({ boundAgentId: "agent_2", status: "idle", sessionStatus: "idle" });
      await flush();
      expect(send).toHaveBeenCalledWith(
        "Later",
        "agent_2",
        [],
        expect.objectContaining({ pinnedConversationId: "conv_elsewhere", stableId: id }),
      );
    } finally {
      conversationRegistry.release("conv_elsewhere");
    }
  });
});
