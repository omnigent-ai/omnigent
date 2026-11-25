import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./sessionsApi";
import { SIDE_CHAT_LABEL_KEY } from "./sessionListCache";
import {
  cancelSessionTimer,
  isRepeatingTimer,
  listSessionLoops,
  sessionTakesSavedTimers,
  setSessionTimer,
  TIMER_NOT_SAVED_CODE,
} from "./sessionTimersApi";
import type { ScheduledTask } from "./scheduledTasksApi";

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
};

describe("setSessionTimer", () => {
  it("posts the timer and maps the saved reply", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(SAVED, 201));
    const timer = await setSessionTimer("conv_a", { seconds: 300, repeat: true, note: "check CI" });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_a/timers");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ seconds: 300, repeat: true, note: "check CI" });
    expect(timer).toEqual({
      timerId: "st_timer",
      fireAt: "2026-11-25T10:05:00+00:00",
      intervalSeconds: 300,
      seconds: 300,
      repeat: true,
      note: "check CI",
    });
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

function task(overrides: Partial<ScheduledTask>): ScheduledTask {
  return {
    id: "st_1",
    kind: "timer",
    state: "active",
    rrule: "FREQ=SECONDLY;INTERVAL=300",
    sessionId: "conv_a",
    ...overrides,
  } as ScheduledTask;
}

describe("listSessionLoops", () => {
  it("asks for every kind and keeps the session's active repeating timers", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        scheduled_tasks: [
          {
            id: "st_loop",
            kind: "timer",
            state: "active",
            rrule: "FREQ=SECONDLY;INTERVAL=300",
            session_id: "conv_a",
            name: "Timer: check CI",
            prompt: "",
            workspace: null,
          },
          {
            id: "st_once",
            kind: "timer",
            state: "active",
            rrule: "FREQ=SECONDLY;INTERVAL=300;COUNT=1",
            session_id: "conv_a",
            name: "Timer",
            prompt: "",
            workspace: null,
          },
          {
            id: "st_other",
            kind: "timer",
            state: "active",
            rrule: "FREQ=SECONDLY;INTERVAL=300",
            session_id: "conv_b",
            name: "Timer",
            prompt: "",
            workspace: null,
          },
          {
            id: "st_auto",
            kind: "automation",
            state: "active",
            rrule: "FREQ=DAILY",
            session_id: "conv_a",
            name: "Nightly",
            prompt: "x",
            workspace: null,
          },
        ],
      }),
    );
    const loops = await listSessionLoops("conv_a");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/scheduled-tasks?kind=all");
    expect(loops.map((loop) => loop.id)).toEqual(["st_loop"]);
  });

  it("isRepeatingTimer excludes one-shots, other kinds and inactive timers", () => {
    expect(isRepeatingTimer(task({}))).toBe(true);
    expect(isRepeatingTimer(task({ rrule: "FREQ=SECONDLY;INTERVAL=300;COUNT=1" }))).toBe(false);
    expect(isRepeatingTimer(task({ kind: "reminder" }))).toBe(false);
    expect(isRepeatingTimer(task({ state: "paused" }))).toBe(false);
    expect(isRepeatingTimer(task({ rrule: null }))).toBe(false);
  });
});
