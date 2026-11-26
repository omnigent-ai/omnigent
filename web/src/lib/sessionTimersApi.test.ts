import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./sessionsApi";
import {
  cancelSessionTimer,
  INPUT_DENIED_CODE,
  runSessionTimerMutation,
  sendLoopPrompt,
  setSessionTimer,
  stopSessionLoop,
  TIMER_NOT_SAVED_CODE,
} from "./sessionTimersApi";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const SAVED = {
  durable: true,
  timer_id: "st_timer",
  status: "scheduled",
  fire_at: "2026-11-25T10:05:00+00:00",
  interval_seconds: 300,
  seconds: 300,
  repeat: true,
  note: "check CI",
  origin: "loop",
  replaced_timer_id: null,
};

describe("sendLoopPrompt", () => {
  it("posts the prompt as a user message tagged with the loop origin", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ queued: true, item_id: "msg_1" }, 202));
    await sendLoopPrompt("sess", "check CI");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/sess/events");
    // Nothing resends it when the host is offline, so the send must not say it will.
    expect(new Headers(init.headers).has("x-omnigent-hold-when-host-offline")).toBe(false);
    expect(JSON.parse(init.body as string)).toEqual({
      type: "message",
      data: {
        role: "user",
        origin: "loop",
        content: [{ type: "input_text", text: "check CI" }],
      },
    });
  });

  it("throws when a session policy refuses the prompt, which the server reports as a success", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ queued: false, denied: true }, 200));
    await expect(sendLoopPrompt("sess", "check CI")).rejects.toMatchObject({
      code: INPUT_DENIED_CODE,
    });
  });
});

const loopInput = { seconds: 300, repeat: true, note: "check CI", origin: "loop" };

describe("setSessionTimer", () => {
  it.each([
    ["no loop", null],
    ["the loop it replaced", "st_old"],
  ])("posts the timer with its origin and maps the saved reply, naming %s", async (_, replaced) => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ...SAVED, replaced_timer_id: replaced }, 201));
    const timer = await setSessionTimer("conv_a", loopInput);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_a/timers");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual(loopInput);
    expect(timer).toEqual({
      timerId: "st_timer",
      fireAt: "2026-11-25T10:05:00+00:00",
      intervalSeconds: 300,
      replacedTimerId: replaced,
    });
  });

  it("throws the server's reason when it keeps no timers for the session", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ durable: false, reason: "a sub-agent's or side chat's timers aren't saved" }),
    );
    const error = await setSessionTimer("conv_a", loopInput).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      code: TIMER_NOT_SAVED_CODE,
      message: "a sub-agent's or side chat's timers aren't saved",
    });
  });

  it("throws the server's error for a refused timer", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: { code: "invalid_input", message: "This session is archived" } }, 400),
    );
    await expect(setSessionTimer("conv_a", loopInput)).rejects.toMatchObject({
      status: 400,
    });
  });
});

describe("cancelSessionTimer", () => {
  it("deletes the timer and reports whether it was still there", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ timer_id: "st_timer", status: "cancelled" }));
    await expect(cancelSessionTimer("conv_a", "st_timer")).resolves.toBe(true);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_a/timers/st_timer");
    expect(init.method).toBe("DELETE");

    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "Timer not found" }, 404));
    await expect(cancelSessionTimer("conv_a", "st_gone")).resolves.toBe(false);
  });
});

describe("stopSessionLoop", () => {
  it("asks the server to stop the caller's loop and names the cancelled timer", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ timer_id: "st_loop", status: "cancelled" }));
    await expect(stopSessionLoop("conv_a")).resolves.toBe("st_loop");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_a/timers?origin=loop");
    expect(init.method).toBe("DELETE");

    fetchMock.mockResolvedValueOnce(jsonResponse({ timer_id: null, status: "none" }));
    await expect(stopSessionLoop("conv_a")).resolves.toBeNull();
  });

  it("throws the server's error when the stop is refused", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "Session not found" }, 404));
    await expect(stopSessionLoop("conv_a")).rejects.toMatchObject({ status: 404 });
  });
});

describe("runSessionTimerMutation", () => {
  it("runs a session's mutations one after another, and keeps going after a failure", async () => {
    const order: string[] = [];
    let releaseFirst!: () => void;
    const first = runSessionTimerMutation("conv_a", async () => {
      await new Promise<void>((resolve) => {
        releaseFirst = resolve;
      });
      order.push("first");
      throw new Error("first failed");
    });
    const second = runSessionTimerMutation("conv_a", async () => {
      order.push("second");
      return "done";
    });
    // Another session is not held up.
    await expect(runSessionTimerMutation("conv_b", async () => "other")).resolves.toBe("other");
    expect(order).toEqual([]);
    releaseFirst();
    await expect(first).rejects.toThrow("first failed");
    await expect(second).resolves.toBe("done");
    expect(order).toEqual(["first", "second"]);
  });
});
