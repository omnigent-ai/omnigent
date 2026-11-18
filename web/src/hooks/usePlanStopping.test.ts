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

function pendingSend(tempId: string, text: string) {
  return { tempId, content: [{ type: "input_text" as const, text }] };
}

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

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  usePlanStops.setState({ requests: {}, settledStops: {} });
});

describe("usePlanStopping", () => {
  it("stops from the Stop until the session stops working, and not on a later wake", async () => {
    const entry = conversationRegistry.acquire(SESSION);
    const { result, rerender } = renderStopping(true);
    expect(result.current).toBe(false);

    const stop = sendStop();
    expect(result.current).toBe(true);
    await stop.take();
    act(() => entry.setState({ blocks: [userMessage("stop", stopPlanMessage(null))] }));
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

  it("keeps stopping when a message sent before the Stop lands after it", async () => {
    const { result } = renderStopping(true);
    const endStop = supersedePlanStop(SESSION);
    const stop = sendStop();
    await stop.take();
    act(endStop);
    expect(result.current).toBe(true);
  });

  it("drops the Stop once no surface watches the session, which can't see it stop", async () => {
    const watching = renderStopping(true);
    await sendStop().take();
    expect(watching.result.current).toBe(true);
    watching.unmount();
    expect(usePlanStops.getState().requests[SESSION]).toBeUndefined();

    expect(renderStopping(true).result.current).toBe(false);
  });

  it("reads a Stop from the transcript after a reload, until the session is seen to stop", () => {
    const entry = conversationRegistry.acquire(SESSION);
    entry.setState({ blocks: [userMessage("stop", stopPlanMessage(null))] });
    const { result, rerender } = renderStopping(true);
    expect(result.current).toBe(true);
    rerender({ working: false });
    expect(result.current).toBe(false);
    rerender({ working: true });
    expect(result.current).toBe(false);

    // A new Stop is its own.
    act(() =>
      entry.setState({
        blocks: [
          userMessage("stop", stopPlanMessage(null)),
          userMessage("again", stopPlanMessage(null)),
        ],
      }),
    );
    expect(result.current).toBe(true);
  });
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

  it("ends with a later message or command from the user, sent or still on its way", () => {
    expect(latestStopRequest([stop, userMessage("go", continuePlanMessage(null))], [])).toBeNull();
    expect(latestStopRequest([stop, slashCommand("cmd", "workflow")], [])).toBeNull();
    expect(latestStopRequest([stop], [pendingSend("pend_1", "Keep the logs")])).toBeNull();
    expect(
      latestStopRequest(
        [userMessage("go", continuePlanMessage(null))],
        [pendingSend("pend_2", stopPlanMessage(null))],
      ),
    ).toBe("pend_2");
    expect(latestStopRequest([], [])).toBeNull();
  });
});
