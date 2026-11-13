import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type ChildSessionInfo, childSessionsQueryKey } from "@/hooks/useChildSessions";
import {
  applyFrameToChildSessions,
  type ChildFrameState,
  createChildFrameState,
  createChildSessionsRefetcher,
  removeIdsFromChildSessions,
  settleFetchedChildSessions,
} from "./childSessionFrames";
import type { SessionListWireItem } from "./sessionListCache";

function child(id: string, overrides: Partial<ChildSessionInfo> = {}): ChildSessionInfo {
  return {
    id,
    title: `worker:${id}`,
    task_summary: null,
    tool: "worker",
    session_name: id,
    labels: {},
    current_task_status: "completed",
    last_task_error: null,
    busy: false,
    last_message_preview: `preview of ${id}`,
    pending_elicitations_count: 0,
    routed_model: null,
    ...overrides,
  };
}

// A frame row for a child, shaped like the server's full-row dump.
function frameRow(
  id: string,
  parentId: string,
  overrides: Partial<SessionListWireItem> = {},
): SessionListWireItem {
  return {
    id,
    title: `worker:${id}`,
    labels: {},
    status: "idle",
    own_status: "idle",
    last_task_error: null,
    pending_elicitations_count: 0,
    child_session_count: 0,
    archived: false,
    parent_session_id: parentId,
    ...overrides,
  };
}

function rows(client: QueryClient, parentId: string): ChildSessionInfo[] | undefined {
  return client.getQueryData<ChildSessionInfo[]>(childSessionsQueryKey(parentId));
}

describe("applyFrameToChildSessions", () => {
  let client: QueryClient;
  let counts: ChildFrameState;

  beforeEach(() => {
    client = new QueryClient();
    counts = createChildFrameState();
    client.setQueryData(childSessionsQueryKey("conv_p"), [child("c1"), child("c2")]);
  });

  it("patches status in place without asking for a refetch", () => {
    const before = rows(client, "conv_p")!;
    const refetch = applyFrameToChildSessions(
      client,
      [frameRow("c1", "conv_p", { own_status: "running", status: "running" })],
      counts,
    );

    expect([...refetch]).toEqual([]);
    const after = rows(client, "conv_p")!;
    expect(after[0]).toMatchObject({ busy: true, current_task_status: "in_progress" });
    // Fields the frame doesn't carry survive the patch.
    expect(after[0].last_message_preview).toBe("preview of c1");
    // The untouched sibling keeps its identity (useDescendantSession relies on it).
    expect(after[1]).toBe(before[1]);
  });

  it("reads busy from own_status, not the rolled-up status", () => {
    // A child whose own sub-agent is running rolls up to status=running, but
    // the child itself is idle.
    applyFrameToChildSessions(
      client,
      [frameRow("c1", "conv_p", { status: "running", own_status: "idle" })],
      counts,
    );

    expect(rows(client, "conv_p")![0]).toMatchObject({
      busy: false,
      current_task_status: "completed",
    });
  });

  it("patches the error, failure status, and pending prompts", () => {
    applyFrameToChildSessions(
      client,
      [
        frameRow("c1", "conv_p", {
          own_status: "failed",
          last_task_error: { code: "runner_disconnected", message: "gone" },
          pending_elicitations_count: 2,
        }),
      ],
      counts,
    );

    expect(rows(client, "conv_p")![0]).toMatchObject({
      busy: false,
      current_task_status: "failed",
      last_task_error: { code: "runner_disconnected", message: "gone" },
      pending_elicitations_count: 2,
    });
  });

  it("leaves the list object alone when a frame restates the cached row", () => {
    const before = rows(client, "conv_p");
    const refetch = applyFrameToChildSessions(client, [frameRow("c1", "conv_p")], counts);

    expect([...refetch]).toEqual([]);
    expect(rows(client, "conv_p")).toBe(before);
  });

  it("asks for a refetch when a turn finishes, for the new preview", () => {
    client.setQueryData(childSessionsQueryKey("conv_p"), [child("c1", { busy: true })]);
    const refetch = applyFrameToChildSessions(client, [frameRow("c1", "conv_p")], counts);

    expect([...refetch]).toEqual(["conv_p"]);
    expect(rows(client, "conv_p")![0].busy).toBe(false);
  });

  it("asks for a refetch for a sub-agent the list doesn't hold yet", () => {
    const refetch = applyFrameToChildSessions(client, [frameRow("c3", "conv_p")], counts);

    expect([...refetch]).toEqual(["conv_p"]);
  });

  it("refetches a renamed child's list now, not in the coalesced batch", () => {
    const refetch = applyFrameToChildSessions(
      client,
      [frameRow("c1", "conv_p", { title: "reviewer:c1" })],
      counts,
    );

    expect([...refetch]).toEqual([]);
    expect([...counts.immediate]).toEqual(["conv_p"]);
    // The title is patched in place, so the frame is not re-announced.
    expect(rows(client, "conv_p")?.[0]?.title).toBe("reviewer:c1");
  });

  it("drops an archived child locally", () => {
    const refetch = applyFrameToChildSessions(
      client,
      [frameRow("c1", "conv_p", { archived: true })],
      counts,
    );

    expect([...refetch]).toEqual([]);
    expect(rows(client, "conv_p")!.map((row) => row.id)).toEqual(["c2"]);
  });

  it("ignores rows whose parent list isn't cached", () => {
    const refetch = applyFrameToChildSessions(client, [frameRow("x1", "conv_cold")], counts);

    expect([...refetch]).toEqual([]);
    expect(rows(client, "conv_cold")).toBeUndefined();
  });

  it("falls back to a refetch for an older server without own_status", () => {
    const legacy = frameRow("c1", "conv_p", { status: "running" });
    delete legacy.own_status;
    const before = rows(client, "conv_p");
    const refetch = applyFrameToChildSessions(client, [legacy], counts);

    expect([...refetch]).toEqual(["conv_p"]);
    expect(rows(client, "conv_p")).toBe(before);
  });

  it("refetches a cached list when its session's child count changes", () => {
    // c1's own list is cached (its row is expanded in the tree).
    client.setQueryData(childSessionsQueryKey("c1"), [child("g1")]);

    // First sight records the baseline only.
    expect([
      ...applyFrameToChildSessions(
        client,
        [frameRow("c1", "conv_p", { child_session_count: 1 })],
        counts,
      ),
    ]).toEqual([]);
    // A natively spawned grandchild is announced only by the count.
    expect([
      ...applyFrameToChildSessions(
        client,
        [frameRow("c1", "conv_p", { child_session_count: 2 })],
        counts,
      ),
    ]).toEqual(["c1"]);
  });

  it("ignores child-count changes for sessions with no cached list", () => {
    applyFrameToChildSessions(
      client,
      [frameRow("c2", "conv_p", { child_session_count: 0 })],
      counts,
    );
    const refetch = applyFrameToChildSessions(
      client,
      [frameRow("c2", "conv_p", { child_session_count: 1 })],
      counts,
    );

    expect([...refetch]).toEqual([]);
  });
});

describe("applyFrameToChildSessions, fields beyond status", () => {
  let client: QueryClient;
  let state: ChildFrameState;

  beforeEach(() => {
    client = new QueryClient();
    state = createChildFrameState();
    client.setQueryData(childSessionsQueryKey("conv_p"), [child("c1")]);
  });

  it("patches a task summary written while the child runs", () => {
    const refetch = applyFrameToChildSessions(
      client,
      [frameRow("c1", "conv_p", { task_summary: "Investigate auth flow" })],
      state,
    );

    expect([...refetch]).toEqual([]);
    expect(rows(client, "conv_p")![0].task_summary).toBe("Investigate auth flow");
  });

  it("refetches when a label the summary displays changes", () => {
    const wrapper = { "omnigent.wrapper": "codex-native-ui" };
    applyFrameToChildSessions(client, [frameRow("c1", "conv_p", { labels: wrapper })], state);
    expect([
      ...applyFrameToChildSessions(client, [frameRow("c1", "conv_p", { labels: wrapper })], state),
    ]).toEqual([]);
    expect([
      ...applyFrameToChildSessions(
        client,
        [
          frameRow("c1", "conv_p", {
            labels: { ...wrapper, "omnigent.routing.decision_id": "d1" },
          }),
        ],
        state,
      ),
    ]).toEqual(["conv_p"]);
  });

  it("patches a Codex child's tool in place on a rename, without refetching", () => {
    const codex = (nickname: string) => ({
      "omnigent.wrapper": "codex-native-ui-subagent",
      "omnigent.codex_native.agent_nickname": nickname,
      "omnigent.codex_native.agent_role": "worker",
    });
    client.setQueryData(childSessionsQueryKey("conv_p"), [
      child("c1", { tool: "planner", labels: codex("planner") }),
      child("c2"),
    ]);
    applyFrameToChildSessions(
      client,
      [frameRow("c1", "conv_p", { labels: codex("planner") })],
      state,
    );
    const untouched = rows(client, "conv_p")?.[1];

    const refetch = applyFrameToChildSessions(
      client,
      [frameRow("c1", "conv_p", { labels: codex("reviewer renamed") })],
      state,
    );

    expect([...refetch]).toEqual([]);
    expect(rows(client, "conv_p")?.[0]?.tool).toBe("reviewer renamed");
    expect(rows(client, "conv_p")?.[1]).toBe(untouched);

    // A label the summary reads that the patch can't place still refetches.
    expect([
      ...applyFrameToChildSessions(
        client,
        [
          frameRow("c1", "conv_p", {
            labels: { ...codex("reviewer renamed"), "omnigent.routing.decision_id": "d1" },
          }),
        ],
        state,
      ),
    ]).toEqual(["conv_p"]);
  });

  it("does not refetch when only usage and cost labels change", () => {
    // Native children rewrite these on every model call.
    const labels = (tokens: string, cost: string) => ({
      "omnigent.wrapper": "claude-native-ui",
      "omnigent.last_context_tokens": tokens,
      "omnigent.cost_usd": cost,
    });
    applyFrameToChildSessions(
      client,
      [frameRow("c1", "conv_p", { labels: labels("100", "0.01") })],
      state,
    );
    for (const [tokens, cost] of [
      ["200", "0.02"],
      ["300", "0.03"],
      ["400", "0.04"],
    ]) {
      const refetch = applyFrameToChildSessions(
        client,
        [frameRow("c1", "conv_p", { labels: labels(tokens, cost) })],
        state,
      );
      expect([...refetch]).toEqual([]);
    }
  });

  it("ignores failure labels cleared at the start of a turn", () => {
    // The error itself is patched from last_task_error.
    applyFrameToChildSessions(client, [frameRow("c1", "conv_p", { labels: { a: "1" } })], state);
    const refetch = applyFrameToChildSessions(
      client,
      [
        frameRow("c1", "conv_p", {
          labels: { a: "1", "omnigent.last_task_error_code": "", other: "" },
        }),
      ],
      state,
    );

    expect([...refetch]).toEqual([]);
  });

  it("refetches an unarchived child the list no longer holds", () => {
    client.setQueryData(childSessionsQueryKey("conv_p"), []);
    const refetch = applyFrameToChildSessions(
      client,
      [frameRow("c1", "conv_p", { archived: false })],
      state,
    );

    expect([...refetch]).toEqual(["conv_p"]);
  });

  it("refetches cached lists on an older server's snapshot", () => {
    // Without child_session_count a snapshot (e.g. after a reconnect) is the
    // only chance to catch children added while disconnected.
    const legacyParent: SessionListWireItem = { id: "conv_p", status: "idle" };
    expect([...applyFrameToChildSessions(client, [legacyParent], state)]).toEqual([]);
    expect([
      ...applyFrameToChildSessions(client, [legacyParent], state, { snapshot: true }),
    ]).toEqual(["conv_p"]);
  });
});

describe("settleFetchedChildSessions", () => {
  it("seeds the child-count baseline from the fetched list", () => {
    const client = new QueryClient();
    const state = createChildFrameState();
    client.setQueryData(childSessionsQueryKey("conv_p"), [child("c1")]);
    client.setQueryData(childSessionsQueryKey("c1"), [child("g1")]);

    settleFetchedChildSessions(client, state, "c1");
    // The first frame already counts a natively spawned second grandchild.
    const refetch = applyFrameToChildSessions(
      client,
      [frameRow("c1", "conv_p", { child_session_count: 2 })],
      state,
    );

    expect(state.counts.get("c1")).toBe(2);
    expect([...refetch]).toEqual(["c1"]);
  });

  it("skips the baseline for a full page, which may be truncated", () => {
    const client = new QueryClient();
    const state = createChildFrameState();
    client.setQueryData(
      childSessionsQueryKey("c1"),
      Array.from({ length: 20 }, (_, i) => child(`g${i}`)),
    );

    settleFetchedChildSessions(client, state, "c1");

    expect(state.counts.has("c1")).toBe(false);
  });

  it("re-applies a frame that arrived while the fetch was in flight", () => {
    const client = new QueryClient();
    const state = createChildFrameState();
    client.setQueryData(childSessionsQueryKey("conv_p"), [child("c1")]);
    state.fetchStarted.set("conv_p", { at: 1_000 });
    applyFrameToChildSessions(
      client,
      [frameRow("c1", "conv_p", { own_status: "running" })],
      state,
      { now: 1_500 },
    );
    // The response was read before the child started; it lands afterwards.
    client.setQueryData(childSessionsQueryKey("conv_p"), [child("c1", { busy: false })]);

    settleFetchedChildSessions(client, state, "conv_p");

    expect(rows(client, "conv_p")![0].busy).toBe(true);
    expect(state.fetchStarted.has("conv_p")).toBe(false);
  });

  it("asks for a reconcile when a count streamed during the fetch disagrees with the list", () => {
    const client = new QueryClient();
    const state = createChildFrameState();
    client.setQueryData(childSessionsQueryKey("conv_p"), []);
    state.fetchStarted.set("conv_p", { at: 1_000 });
    applyFrameToChildSessions(client, [frameRow("conv_p", "", { child_session_count: 1 })], state, {
      now: 1_500,
    });

    expect(settleFetchedChildSessions(client, state, "conv_p")).toBe(true);
    expect(state.counts.get("conv_p")).toBe(1);
  });

  it("does not ask for a reconcile when the fetched list matches the streamed count", () => {
    const client = new QueryClient();
    const state = createChildFrameState();
    client.setQueryData(childSessionsQueryKey("conv_p"), [child("c1")]);
    state.fetchStarted.set("conv_p", { at: 1_000 });
    applyFrameToChildSessions(client, [frameRow("conv_p", "", { child_session_count: 1 })], state, {
      now: 1_500,
    });

    expect(settleFetchedChildSessions(client, state, "conv_p")).toBe(false);
  });

  it("keeps the fetched row over a frame from before the fetch", () => {
    const client = new QueryClient();
    const state = createChildFrameState();
    client.setQueryData(childSessionsQueryKey("conv_p"), [child("c1")]);
    applyFrameToChildSessions(
      client,
      [frameRow("c1", "conv_p", { own_status: "running" })],
      state,
      { now: 500 },
    );
    state.fetchStarted.set("conv_p", { at: 1_000 });
    client.setQueryData(childSessionsQueryKey("conv_p"), [child("c1", { busy: false })]);
    const fetched = rows(client, "conv_p");

    settleFetchedChildSessions(client, state, "conv_p");

    expect(rows(client, "conv_p")).toBe(fetched);
    expect(fetched![0].busy).toBe(false);
  });
});

describe("removeIdsFromChildSessions", () => {
  it("drops removed sessions from every cached list", () => {
    const client = new QueryClient();
    client.setQueryData(childSessionsQueryKey("conv_p"), [child("c1"), child("c2")]);
    client.setQueryData(childSessionsQueryKey("c1"), [child("g1")]);
    const untouched = [child("g2")];
    client.setQueryData(childSessionsQueryKey("c2"), untouched);

    removeIdsFromChildSessions(client, ["c1", "g1"]);

    expect(rows(client, "conv_p")!.map((row) => row.id)).toEqual(["c2"]);
    expect(rows(client, "c1")).toEqual([]);
    expect(rows(client, "c2")).toBe(untouched);
  });
});

describe("createChildSessionsRefetcher flush", () => {
  it("refetches a parent now and drops its pending delayed refetch", () => {
    vi.useFakeTimers();
    try {
      const client = new QueryClient();
      const invalidate = vi.spyOn(client, "invalidateQueries");
      const refetcher = createChildSessionsRefetcher(client, 1_000);

      refetcher.schedule("conv_p");
      refetcher.flush("conv_p");
      expect(invalidate).toHaveBeenCalledTimes(1);

      vi.advanceTimersByTime(5_000);
      expect(invalidate).toHaveBeenCalledTimes(1);
      refetcher.dispose();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("createChildSessionsRefetcher", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("coalesces a burst into one refetch per parent after the delay", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const refetcher = createChildSessionsRefetcher(client, 1_000);

    refetcher.schedule("conv_p");
    refetcher.schedule("conv_p");
    refetcher.schedule("c1");
    vi.advanceTimersByTime(999);
    expect(invalidate).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1);
    expect(invalidate).toHaveBeenCalledTimes(2);
    expect(invalidate).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: childSessionsQueryKey("conv_p"), exact: true }),
    );

    // A later request starts a new window.
    refetcher.schedule("conv_p");
    vi.advanceTimersByTime(1_000);
    expect(invalidate).toHaveBeenCalledTimes(3);
  });

  it("refetches a list nothing currently displays", async () => {
    // Unmounted lists don't refetch on remount, so they must refetch here.
    vi.useRealTimers();
    const client = new QueryClient();
    const queryFn = vi.fn(async () => [child("c1")]);
    const observer = new QueryObserver(client, {
      queryKey: childSessionsQueryKey("conv_p"),
      queryFn,
    });
    const unsubscribe = observer.subscribe(() => {});
    await vi.waitFor(() => expect(observer.getCurrentResult().isSuccess).toBe(true));
    unsubscribe();

    const refetcher = createChildSessionsRefetcher(client, 10);
    refetcher.schedule("conv_p");

    await vi.waitFor(() => expect(queryFn).toHaveBeenCalledTimes(2));
  });

  it("cancels pending refetches on dispose", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const refetcher = createChildSessionsRefetcher(client, 1_000);

    refetcher.schedule("conv_p");
    refetcher.dispose();
    vi.advanceTimersByTime(5_000);

    expect(invalidate).not.toHaveBeenCalled();
  });
});
