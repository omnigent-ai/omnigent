import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { AnyBlock } from "@/lib/blocks";
import { continuePlanMessage, stopPlanMessage } from "@/lib/workflow";
import { conversationRegistry } from "@/store/conversationRegistry";
import { supersedePlanStop, trackPlanStop, usePlanStops } from "@/store/planStops";
import { latestStopRequest, usePlanStopping } from "./usePlanStopping";

const SESSION = "e".repeat(32);

function userMessage(itemId: string, text: string): AnyBlock {
  return {
    type: "user_message",
    ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "turn", itemId },
    content: [{ type: "input_text", text }],
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

const STOP = userMessage("stop", stopPlanMessage(null));
const CONTINUE = userMessage("go", continuePlanMessage(null));

/** A stop whose message is still on its way, and how to settle it. */
function sendStop() {
  let settle!: (taken: boolean) => void;
  let fail!: (error: Error) => void;
  let tracked!: Promise<void>;
  act(() => {
    tracked = trackPlanStop(
      SESSION,
      () =>
        new Promise<boolean>((resolve, reject) => {
          settle = resolve;
          fail = reject;
        }),
    );
  });
  const settleWith = (taken: boolean) =>
    act(async () => {
      settle(taken);
      await tracked;
    });
  return {
    take: () => settleWith(true),
    refuse: () => settleWith(false),
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
    await stop.take();
    act(() => entry.setState({ blocks: [STOP] }));
    expect(result.current).toBe(true);

    rerender({ working: false });
    expect(result.current).toBe(false);
    expect(usePlanStops.getState().requests[SESSION]).toBeUndefined();
    // The Stop is still the latest request, but a later wake, such as a timer, isn't its.
    rerender({ working: true });
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

    const stop = sendStop();
    await stop.take();
    act(() => supersedePlanStop(SESSION)());
    expect(result.current).toBe(false);

    // Continue lands while the Stop is still on its way; the Stop doesn't come back.
    const late = sendStop();
    const endStop = supersedePlanStop(SESSION);
    act(endStop);
    await late.take();
    expect(result.current).toBe(false);
  });

  it("keeps stopping when a message sent before a Stop lands after it", async () => {
    const { result } = renderStopping(true);
    const endNoStop = supersedePlanStop(SESSION);
    await sendStop().take();
    act(endNoStop);
    expect(result.current).toBe(true);

    // Stop A, then a follow-up, then Stop B: the follow-up only ends A.
    const endStopA = supersedePlanStop(SESSION);
    await sendStop().take();
    act(endStopA);
    expect(result.current).toBe(true);
    expect(usePlanStops.getState().requests[SESSION]).toMatchObject({ sent: true });
  });

  it("ends when the transcript shows a later request after the stop message, as from another tab", async () => {
    const entry = transcript();
    const { result } = renderStopping(true);
    // The delivery names no item, as a native session's doesn't.
    await sendStop().take();
    act(() => entry.setState({ blocks: [STOP] }));
    act(() =>
      entry.setState({
        blocks: [STOP, userMessage("report", "[Message from session 9b8b]\nStopped my shells.")],
      }),
    );
    expect(result.current).toBe(true);

    // A transcript with no requests, as once its stream closes, isn't a later request.
    act(() => entry.setState({ blocks: [] }));
    expect(result.current).toBe(true);

    // Background work keeps the session working throughout.
    act(() => entry.setState({ blocks: [STOP, CONTINUE] }));
    expect(result.current).toBe(false);
    expect(usePlanStops.getState().requests[SESSION]).toBeUndefined();
  });

  it("follows the order the session took requests in, not when their sends were accepted", async () => {
    const entry = transcript();
    const { result } = renderStopping(true);
    await sendStop().take();
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

  it("drops the Stop once no surface watches the session, which can't see it stop", async () => {
    const first = renderStopping(true);
    const second = renderStopping(true);
    await sendStop().take();
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
    act(() => entry.setState({ blocks: [STOP, userMessage("again", stopPlanMessage(null))] }));
    expect(result.current).toBe(true);
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
    ).toBe("stop");
  });

  it("ends with a later message or command from the user, once the session has it", () => {
    expect(latestStopRequest([stop, CONTINUE], [])).toBeNull();
    expect(latestStopRequest([stop, slashCommand("cmd", "workflow")], [])).toBeNull();
    expect(latestStopRequest([stop], [pendingSend("pend_1", "Keep the logs", true)])).toBeNull();
    // A send the session hasn't accepted doesn't count yet.
    expect(latestStopRequest([stop], [pendingSend("pend_1", "Keep the logs")])).toBe("stop");
    expect(
      latestStopRequest([CONTINUE], [pendingSend("pend_2", stopPlanMessage(null), true)]),
    ).toBe("pend_2");
    expect(latestStopRequest([], [])).toBeUndefined();
  });
});
