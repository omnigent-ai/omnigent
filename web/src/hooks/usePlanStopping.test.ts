import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { AnyBlock } from "@/lib/blocks";
import { continuePlanMessage, stopPlanMessage } from "@/lib/workflow";
import { conversationRegistry } from "@/store/conversationRegistry";
import {
  hasRequestAfter,
  latestStopRequest,
  type MessageReceipt,
  notePlanStopReceipt,
  supersedePlanStop,
  trackPlanStop,
  usePlanStops,
} from "@/store/planStops";
import { usePlanStopping } from "./usePlanStopping";

const SESSION = "e".repeat(32);

function userMessage(itemId: string, text: string, stableKey?: string): AnyBlock {
  return {
    type: "user_message",
    ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "turn", itemId },
    content: [{ type: "input_text", text }],
    ...(stableKey ? { stableKey } : {}),
  };
}

function slashCommand(itemId: string, name: string): AnyBlock {
  return {
    type: "slash_command",
    ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "turn", itemId },
    kind: "skill",
    name,
    arguments: "",
    output: null,
  };
}

/** A send the composer shows, `posted` once the session accepted it. */
function pendingSend(tempId: string, text: string, posted?: boolean) {
  return {
    tempId,
    content: [{ type: "input_text" as const, text }],
    ...(posted === undefined ? {} : { posted }),
  };
}

const stopMessage = (itemId: string, stableKey?: string) =>
  userMessage(itemId, stopPlanMessage(null), stableKey);
const STOP = stopMessage("stop");
const CONTINUE = userMessage("go", continuePlanMessage(null));

/** A stop whose message is still on its way, and how to settle it. */
function sendStop() {
  let settle!: (receipt: MessageReceipt | null) => void;
  let fail!: (error: Error) => void;
  let tracked!: Promise<void>;
  act(() => {
    tracked = trackPlanStop(
      SESSION,
      () =>
        new Promise<MessageReceipt | null>((resolve, reject) => {
          settle = resolve;
          fail = reject;
        }),
    );
  });
  const settleWith = (receipt: MessageReceipt | null) =>
    act(async () => {
      settle(receipt);
      await tracked;
    });
  return {
    take: (receipt: MessageReceipt = {}) => settleWith(receipt),
    refuse: () => settleWith(null),
    fail: (error: Error) =>
      act(async () => {
        fail(error);
        await expect(tracked).rejects.toBe(error);
      }),
  };
}

function renderStopping(working: boolean) {
  return renderHook((props: { working: boolean }) => usePlanStopping(SESSION, props.working), {
    initialProps: { working },
  });
}

/** The session's streamed transcript. */
function transcript() {
  return conversationRegistry.acquire(SESSION);
}

/** A follow-up the session accepts, sent outside the chat, as a run row's Retry is. */
function sendFollowUp() {
  const accept = supersedePlanStop(SESSION);
  return () => act(accept);
}

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  usePlanStops.setState({ requests: {}, settledStops: {} });
});

describe("usePlanStopping", () => {
  it("stops from the Stop until the session stops working, and not on a later wake", async () => {
    const entry = transcript();
    const { result, rerender } = renderStopping(true);
    expect(result.current).toBe(false);

    const stop = sendStop();
    expect(result.current).toBe(true);
    await stop.take({ itemId: "stop" });
    act(() => entry.setState({ blocks: [STOP] }));
    expect(result.current).toBe(true);

    rerender({ working: false });
    expect(result.current).toBe(false);
    expect(usePlanStops.getState().requests[SESSION]).toBeUndefined();
    // The Stop is still the latest request, but a later wake, such as a timer, isn't its.
    rerender({ working: true });
    expect(result.current).toBe(false);
  });

  it("knows a Stop seen to finish without its transcript when the transcript loads later", async () => {
    const { result, rerender } = renderStopping(true);
    await sendStop().take({ itemId: "stop" });
    rerender({ working: false });
    expect(result.current).toBe(false);

    // The session wakes from a worker's report, then its transcript loads.
    rerender({ working: true });
    act(() => transcript().setState({ blocks: [STOP] }));
    expect(result.current).toBe(false);
  });

  it("restores the controls when the session refuses the Stop or it doesn't arrive", async () => {
    const { result } = renderStopping(true);

    const refused = sendStop();
    expect(result.current).toBe(true);
    await refused.refuse();
    expect(result.current).toBe(false);

    const failed = sendStop();
    expect(result.current).toBe(true);
    await failed.fail(new Error("The session is offline."));
    expect(result.current).toBe(false);
    expect(usePlanStops.getState().requests[SESSION]).toBeUndefined();
  });

  it("ends once the session takes a message sent after the Stop, even mid-send", async () => {
    const { result } = renderStopping(true);

    await sendStop().take();
    await sendFollowUp()();
    expect(result.current).toBe(false);

    // Continue lands while the Stop is still on its way; the Stop doesn't come back.
    const late = sendStop();
    await sendFollowUp()();
    await late.take();
    expect(result.current).toBe(false);
  });

  it("keeps stopping when a message sent before a Stop lands after it", async () => {
    const { result } = renderStopping(true);
    const acceptEarlier = sendFollowUp();
    await sendStop().take();
    await acceptEarlier();
    expect(result.current).toBe(true);

    // Stop A, then a follow-up, then Stop B: the follow-up only ends A.
    const acceptAfterA = sendFollowUp();
    await sendStop().take();
    await acceptAfterA();
    expect(result.current).toBe(true);
    expect(usePlanStops.getState().requests[SESSION]).toMatchObject({ sent: true });
  });

  it("keeps Stop B through Stop A's and an earlier follow-up's items arriving later", async () => {
    const entry = transcript();
    const { result } = renderStopping(true);
    // Stop A and a follow-up are accepted before either item arrives.
    await sendStop().take({ itemId: "stop_a" });
    await sendFollowUp()();
    expect(result.current).toBe(false);
    await sendStop().take({ itemId: "stop_b" });
    expect(result.current).toBe(true);

    const logs = userMessage("logs", "Keep the logs");
    act(() => entry.setState({ blocks: [stopMessage("stop_a")] }));
    expect(result.current).toBe(true);
    act(() => entry.setState({ blocks: [stopMessage("stop_a"), logs] }));
    expect(result.current).toBe(true);
    act(() => entry.setState({ blocks: [stopMessage("stop_a"), logs, stopMessage("stop_b")] }));
    expect(result.current).toBe(true);
    act(() =>
      entry.setState({ blocks: [stopMessage("stop_a"), logs, stopMessage("stop_b"), CONTINUE] }),
    );
    expect(result.current).toBe(false);
  });

  it("keeps a Stop that began while the transcript ended in an older stop message", async () => {
    const entry = transcript();
    entry.setState({ blocks: [STOP] });
    const { result } = renderStopping(true);
    // A composer follow-up the session accepted; its item comes later.
    const accept = supersedePlanStop(SESSION);
    act(() => {
      accept();
      entry.setState({ pendingUserMessages: [pendingSend("pend_1", "Keep the logs", true)] });
    });
    expect(result.current).toBe(false);

    await sendStop().take({ itemId: "stop_b" });
    act(() =>
      entry.setState({
        blocks: [STOP, userMessage("logs", "Keep the logs")],
        pendingUserMessages: [],
      }),
    );
    expect(result.current).toBe(true);
  });

  it("ends when the transcript shows a request after the Stop's own message, as from another tab", async () => {
    const entry = transcript();
    const { result } = renderStopping(true);
    await sendStop().take({ itemId: "stop" });
    act(() =>
      entry.setState({
        blocks: [STOP, userMessage("report", "[Message from session 9b8b]\nStopped my shells.")],
      }),
    );
    expect(result.current).toBe(true);

    // Background work keeps the session working throughout.
    act(() => entry.setState({ blocks: [STOP, CONTINUE] }));
    expect(result.current).toBe(false);
    expect(usePlanStops.getState().requests[SESSION]).toBeUndefined();
  });

  it("places a native Stop's message by the receipt its pending input gets", async () => {
    const entry = transcript();
    const { result } = renderStopping(true);
    await sendStop().take({ pendingId: "pending_stop" });
    act(() => entry.setState({ blocks: [STOP, CONTINUE] }));
    // Until the receipt names its item, the transcript can't place the Stop.
    expect(result.current).toBe(true);
    act(() => notePlanStopReceipt(SESSION, "pending_stop", "stop"));
    expect(result.current).toBe(false);

    // A receipt that arrives before the delivery's response counts too.
    const again = sendStop();
    act(() => {
      entry.setState({ blocks: [] });
      notePlanStopReceipt(SESSION, "pending_again", "again");
    });
    await again.take({ pendingId: "pending_again" });
    expect(usePlanStops.getState().requests[SESSION]).toMatchObject({ itemId: "again" });
  });

  it("doesn't take a Stop's own message for a stop once a follow-up ended it", async () => {
    const entry = transcript();
    const { result } = renderStopping(true);
    await sendStop().take({ itemId: "stop_b" });
    await sendFollowUp()();
    act(() => entry.setState({ blocks: [stopMessage("stop_b")] }));
    expect(result.current).toBe(false);

    // A native Stop's block can arrive under a follow-up's pending key, before the follow-up lands.
    await sendStop().take({ pendingId: "pending_x" });
    const acceptFollowUp = sendFollowUp();
    act(() => {
      notePlanStopReceipt(SESSION, "pending_x", "stop_x");
      entry.setState({
        blocks: [stopMessage("stop_b"), stopMessage("stop_x", "pend_follow")],
      });
    });
    await acceptFollowUp();
    expect(result.current).toBe(false);

    // A native Stop's message, once its receipt names it.
    await sendStop().take({ pendingId: "pending_c" });
    await sendFollowUp()();
    act(() => {
      notePlanStopReceipt(SESSION, "pending_c", "stop_c");
      entry.setState({ blocks: [stopMessage("stop_b"), stopMessage("stop_c")] });
    });
    expect(result.current).toBe(false);
  });

  it("follows the order the session took requests in, not when their sends were accepted", async () => {
    const entry = transcript();
    const { result } = renderStopping(true);
    await sendStop().take({ itemId: "stop" });
    act(() => entry.setState({ blocks: [STOP] }));

    // A follow-up sent before the Stop is accepted after it.
    act(() =>
      entry.setState({ pendingUserMessages: [pendingSend("pend_1", "Keep the logs", true)] }),
    );
    expect(result.current).toBe(true);
    // The session took it first.
    act(() =>
      entry.setState({
        blocks: [userMessage("logs", "Keep the logs"), STOP],
        pendingUserMessages: [],
      }),
    );
    expect(result.current).toBe(true);
  });

  it("settles only once every surface watching the session sees it stopped", async () => {
    // The Tasks tab counts a busy sub-agent; a run row reads only the session's own activity.
    const tasksTab = renderStopping(true);
    const runRow = renderStopping(true);
    await sendStop().take({ itemId: "stop" });
    runRow.rerender({ working: false });
    expect(tasksTab.result.current).toBe(true);
    expect(usePlanStops.getState().requests[SESSION]).toMatchObject({ sent: true });

    tasksTab.rerender({ working: false });
    expect(usePlanStops.getState().requests[SESSION]).toBeUndefined();
    expect(usePlanStops.getState().settledStops[SESSION]).toContain("stop");
  });

  it("keeps a native Stop A retired and Stop B open as their replies cross", async () => {
    const entry = transcript();
    const { result } = renderStopping(true);
    const stopA = sendStop();
    await sendFollowUp()();
    const stopB = sendStop();
    await stopB.take({ itemId: "stop_b" });
    // A's delivery answers with only a pending input, then its receipt names the item.
    await stopA.take({ pendingId: "pending_a" });
    act(() => notePlanStopReceipt(SESSION, "pending_a", "stop_a"));
    expect(usePlanStops.getState().settledStops[SESSION]).toContain("stop_a");
    expect(usePlanStops.getState().requests[SESSION]).toMatchObject({ itemId: "stop_b" });

    act(() => entry.setState({ blocks: [stopMessage("stop_a")] }));
    expect(result.current).toBe(true);
    act(() => entry.setState({ blocks: [stopMessage("stop_a"), stopMessage("stop_b")] }));
    expect(result.current).toBe(true);
  });

  it("drops the Stop once no surface watches the session, which can't see it stop", async () => {
    const first = renderStopping(true);
    const second = renderStopping(true);
    await sendStop().take({ itemId: "stop" });
    first.unmount();
    expect(second.result.current).toBe(true);
    expect(usePlanStops.getState().requests[SESSION]).toMatchObject({ sent: true });
    second.unmount();
    expect(usePlanStops.getState().requests[SESSION]).toBeUndefined();

    // Unwatched, the run stopped and was continued from another tab; the session runs again.
    transcript().setState({ blocks: [STOP, CONTINUE] });
    expect(renderStopping(true).result.current).toBe(false);
  });

  it("reads a Stop from the transcript after a reload, until the session is seen to stop", () => {
    const entry = transcript();
    entry.setState({ blocks: [STOP] });
    const { result, rerender } = renderStopping(true);
    expect(result.current).toBe(true);
    rerender({ working: false });
    expect(result.current).toBe(false);
    rerender({ working: true });
    expect(result.current).toBe(false);

    // A new Stop is its own.
    act(() => entry.setState({ blocks: [STOP, stopMessage("again")] }));
    expect(result.current).toBe(true);
  });

  it("reads a Stop the server replays as pending after a reload, until a later request is accepted", async () => {
    const entry = transcript();
    entry.setState({
      blocks: [CONTINUE],
      pendingUserMessages: [
        { ...pendingSend("pending_srv", stopPlanMessage(null)), replayed: true },
      ],
    });
    const { result } = renderStopping(true);
    expect(result.current).toBe(true);

    // A run row's Retry is accepted before either message reaches the transcript.
    await sendFollowUp()();
    expect(result.current).toBe(false);
    // The Stop's receipt arrives, and it commits under its own item.
    act(() => {
      notePlanStopReceipt(SESSION, "pending_srv", "stop_srv");
      entry.setState({ blocks: [CONTINUE, stopMessage("stop_srv")], pendingUserMessages: [] });
    });
    expect(result.current).toBe(false);
  });

  it("ends a transcript's Stop once the session accepts a follow-up sent outside the chat", async () => {
    const entry = transcript();
    entry.setState({ blocks: [STOP] });
    const { result } = renderStopping(true);
    expect(result.current).toBe(true);

    // A run row's Retry: no pending bubble, and its item comes later.
    await sendFollowUp()();
    expect(result.current).toBe(false);
  });

  it.each([
    ["message", "Keep the logs", userMessage("logs", "Keep the logs")],
    ["slash command", "/workflow Fix the tests", slashCommand("cmd", "workflow")],
  ])(
    "keeps a transcript's Stop until the session accepts a follow-up %s",
    (_kind, text, committed) => {
      const entry = transcript();
      entry.setState({ blocks: [STOP] });
      const { result } = renderStopping(true);

      act(() => entry.setState({ pendingUserMessages: [pendingSend("pend_1", text)] }));
      expect(result.current).toBe(true);
      // Refused: its bubble goes away.
      act(() => entry.setState({ pendingUserMessages: [] }));
      expect(result.current).toBe(true);

      act(() => entry.setState({ pendingUserMessages: [pendingSend("pend_2", text, true)] }));
      expect(result.current).toBe(false);
      act(() => entry.setState({ blocks: [STOP, committed], pendingUserMessages: [] }));
      expect(result.current).toBe(false);
    },
  );
});

describe("latestStopRequest", () => {
  const stop = userMessage("stop", stopPlanMessage("Daily Sync"));

  it("skips runtime notices and other sessions' reports after the Stop", () => {
    expect(
      latestStopRequest(
        [
          userMessage("start", "Use the workflow skill to run the saved workflow."),
          stop,
          userMessage("report", "[Message from session 9b8b]\nStopped my shells."),
          userMessage("notice", "[System: task call_1 (sub_agent) completed]"),
        ],
        [],
      ),
    ).toEqual(["stop"]);
  });

  it("ends with a later message or command from the user, once the session has it", () => {
    expect(latestStopRequest([stop, CONTINUE], [])).toBeNull();
    expect(latestStopRequest([stop, slashCommand("cmd", "workflow")], [])).toBeNull();
    expect(latestStopRequest([stop], [pendingSend("pend_1", "Keep the logs", true)])).toBeNull();
    // A send the session hasn't accepted doesn't count yet.
    expect(latestStopRequest([stop], [pendingSend("pend_1", "Keep the logs")])).toEqual(["stop"]);
    // After a reload, the server replays sends it accepted that the agent hasn't taken.
    const replayed = (tempId: string, text: string) => ({
      ...pendingSend(tempId, text),
      replayed: true,
    });
    expect(latestStopRequest([CONTINUE], [replayed("pending_srv", stopPlanMessage(null))])).toEqual(
      ["pending_srv"],
    );
    expect(latestStopRequest([stop], [replayed("pending_srv", "Keep the logs")])).toBeNull();
    expect(
      latestStopRequest([CONTINUE], [pendingSend("pend_2", stopPlanMessage(null), true)]),
    ).toEqual(["pend_2"]);
    // A promoted send goes by its pending key as well as its item.
    expect(
      latestStopRequest([userMessage("stop", stopPlanMessage("Daily Sync"), "pend_3")], []),
    ).toEqual(["stop", "pend_3"]);
    expect(latestStopRequest([], [])).toBeUndefined();
  });
});

describe("hasRequestAfter", () => {
  it("finds a request from the user after the item, not a notice or a report", () => {
    expect(hasRequestAfter([STOP], "stop")).toBe(false);
    expect(hasRequestAfter([], "stop")).toBe(false);
    expect(
      hasRequestAfter(
        [
          STOP,
          userMessage("notice", "[System: timer t1 fired]"),
          userMessage("report", "[Message from session 9b8b]\nDone."),
        ],
        "stop",
      ),
    ).toBe(false);
    expect(hasRequestAfter([STOP, slashCommand("cmd", "workflow")], "stop")).toBe(true);
    expect(hasRequestAfter([CONTINUE, STOP], "stop")).toBe(false);
  });
});
