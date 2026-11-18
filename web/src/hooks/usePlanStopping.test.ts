import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { AnyBlock } from "@/lib/blocks";
import { continuePlanMessage, stopPlanMessage } from "@/lib/workflow";
import { conversationRegistry } from "@/store/conversationRegistry";
import { clearPlanStop, trackPlanStop, usePlanStops } from "@/store/planStops";
import { latestRequestIsStop, usePlanStopping } from "./usePlanStopping";

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

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  usePlanStops.setState({ requests: {} });
});

describe("usePlanStopping", () => {
  it("stops from the Stop until the session stops working, then lets the run's controls show", async () => {
    const { result, rerender } = renderHook(({ working }) => usePlanStopping(SESSION, working), {
      initialProps: { working: true },
    });
    expect(result.current).toBe(false);

    const stop = sendStop();
    expect(result.current).toBe(true);
    await stop.take();
    expect(result.current).toBe(true);

    rerender({ working: false });
    expect(result.current).toBe(false);
    expect(usePlanStops.getState().requests[SESSION]).toBeUndefined();
    // A later wake isn't the Stop's: the session shows Stop again.
    rerender({ working: true });
    expect(result.current).toBe(false);
  });

  it("restores the controls when the session refuses the Stop or it doesn't arrive", async () => {
    const { result } = renderHook(() => usePlanStopping(SESSION, true));

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

  it("ends when the session takes another message, such as Continue, even mid-send", async () => {
    const { result } = renderHook(() => usePlanStopping(SESSION, true));

    const stop = sendStop();
    await stop.take();
    act(() => clearPlanStop(SESSION));
    expect(result.current).toBe(false);

    // Continue lands while the Stop is still on its way; the Stop doesn't come back.
    const late = sendStop();
    act(() => clearPlanStop(SESSION));
    await late.take();
    expect(result.current).toBe(false);
  });

  it("reads a Stop from the transcript after a reload, while the session works", () => {
    const entry = conversationRegistry.acquire(SESSION);
    entry.setState({ blocks: [userMessage("stop", stopPlanMessage(null))] });
    const { result, rerender } = renderHook(({ working }) => usePlanStopping(SESSION, working), {
      initialProps: { working: true },
    });
    expect(result.current).toBe(true);
    rerender({ working: false });
    expect(result.current).toBe(false);
  });
});

describe("latestRequestIsStop", () => {
  const stop = userMessage("stop", stopPlanMessage("Daily Sync"));

  it("skips runtime notices and other sessions' reports after the Stop", () => {
    expect(
      latestRequestIsStop(
        [
          userMessage("start", "Use the workflow skill to run the saved workflow."),
          stop,
          userMessage("report", "[Message from session 9b8b]\nStopped my shells."),
          userMessage("notice", "[System: task call_1 (sub_agent) completed]"),
        ],
        [],
      ),
    ).toBe(true);
  });

  it("ends with a later message or command from the user, sent or still on its way", () => {
    expect(latestRequestIsStop([stop, userMessage("go", continuePlanMessage(null))], [])).toBe(
      false,
    );
    expect(latestRequestIsStop([stop, slashCommand("cmd", "workflow")], [])).toBe(false);
    expect(
      latestRequestIsStop([stop], [{ content: [{ type: "input_text", text: "Keep the logs" }] }]),
    ).toBe(false);
    expect(
      latestRequestIsStop(
        [userMessage("go", continuePlanMessage(null))],
        [{ content: [{ type: "input_text", text: stopPlanMessage(null) }] }],
      ),
    ).toBe(true);
    expect(latestRequestIsStop([], [])).toBe(false);
  });
});
