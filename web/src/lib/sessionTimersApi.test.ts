import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./sessionsApi";
import { SIDE_CHAT_LABEL_KEY } from "./sessionListCache";
import {
  cancelSessionTimer,
  listSessionLoops,
  listSessionTimers,
  runSessionTimerMutation,
  sessionTakesSavedTimers,
  setSessionTimer,
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
  note: "Run now: check CI",
  origin: "loop",
};

describe("setSessionTimer", () => {
  it("posts the timer with its origin and maps the saved reply", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(SAVED, 201));
    const timer = await setSessionTimer("conv_a", {
      seconds: 300,
      repeat: true,
      note: "Run now: check CI",
      origin: "loop",
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_a/timers");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      seconds: 300,
      repeat: true,
      note: "Run now: check CI",
      origin: "loop",
    });
    expect(timer).toEqual({
      timerId: "st_timer",
      fireAt: "2026-11-25T10:05:00+00:00",
      intervalSeconds: 300,
      seconds: 300,
      repeat: true,
      note: "Run now: check CI",
      origin: "loop",
    });
  });

  it("omits an unset origin, as the agent's own timers do", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ...SAVED, origin: null }, 201));
    await setSessionTimer("conv_a", { seconds: 300, repeat: true });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ seconds: 300, repeat: true, note: null });
  });

  it("throws the server's reason when it keeps no timers for the session", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ durable: false, reason: "a sub-agent's or side chat's timers aren't saved" }),
    );
    const error = await setSessionTimer("conv_a", { seconds: 300, repeat: true }).catch(
      (e: unknown) => e,
    );
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
    await expect(setSessionTimer("conv_a", { seconds: 300, repeat: true })).rejects.toMatchObject({
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

describe("sessionTakesSavedTimers", () => {
  it("accepts a top-level or unknown session and refuses children and side chats", () => {
    expect(sessionTakesSavedTimers(null)).toBe(true);
    expect(sessionTakesSavedTimers({ parentSessionId: null, labels: {} })).toBe(true);
    expect(sessionTakesSavedTimers({ parentSessionId: "conv_parent" })).toBe(false);
    expect(sessionTakesSavedTimers({ labels: { [SIDE_CHAT_LABEL_KEY]: "1" } })).toBe(false);
  });
});

const LISTED = {
  timers: [
    {
      timer_id: "st_loop",
      origin: "loop",
      name: "Loop: Run now: check CI",
      repeat: true,
      interval_seconds: 300,
      next_run_at: "2026-11-25T10:05:00+00:00",
    },
    // The agent's own repeating timer, and a one-shot: neither is a loop.
    { timer_id: "st_agent", origin: "agent", name: "Timer", repeat: true, interval_seconds: 600 },
    {
      timer_id: "st_once",
      origin: "loop",
      name: "Loop",
      repeat: false,
      interval_seconds: 300,
      next_run_at: null,
    },
  ],
};

describe("listSessionTimers / listSessionLoops", () => {
  it("reads the session's timers from the ungated listing route", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(LISTED));
    const timers = await listSessionTimers("conv_a");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/sessions/conv_a/timers");
    expect(timers[0]).toEqual({
      timerId: "st_loop",
      origin: "loop",
      name: "Loop: Run now: check CI",
      repeat: true,
      intervalSeconds: 300,
      nextRunAt: "2026-11-25T10:05:00+00:00",
    });
    expect(timers[1]).toMatchObject({ timerId: "st_agent", origin: "agent", nextRunAt: null });
  });

  it("keeps only the repeating loop-tagged timers, leaving the agent's alone", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(LISTED));
    const loops = await listSessionLoops("conv_a");
    expect(loops.map((loop) => loop.timerId)).toEqual(["st_loop"]);
  });

  it("throws the server's error when the listing is refused", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "Session not found" }, 404));
    await expect(listSessionLoops("conv_a")).rejects.toMatchObject({ status: 404 });
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
