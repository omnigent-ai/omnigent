import { describe, expect, it } from "vitest";

import type { GitHubBudgetPace } from "@/lib/githubReadBudget";

import type { StackNode, StackParse } from "./stackModel";
import {
  createStackWarmupQueue,
  STACK_WARMUP_CHANGES_PHASE,
  STACK_WARMUP_DETAILS_PHASE,
  STACK_WARMUP_GAP_MS,
  STACK_WARMUP_SLOW_GAP_MS,
  type StackWarmupPacing,
  type StackWarmupRead,
  stackWarmupOrder,
} from "./stackWarmup";

const urlOf = (number: number) => `https://github.com/acme/app/pull/${number}`;

/** A stack of `entries`, each `[depth, number]` base first, read as PR `self`'s. */
function parseOf(entries: [number, number][], self: number): StackParse {
  const parents: number[] = [];
  const nodes: StackNode[] = entries.map(([depth, number], index) => {
    parents[depth] = index;
    return {
      depth,
      parentIndex: depth === 0 ? null : parents[depth - 1]!,
      branch: `stack/${number}`,
      ref: { url: urlOf(number), host: "github.com", repository: "acme/app", number },
      external: false,
      markers: { current: number === self, merged: false },
    };
  });
  return {
    format: "example-stack",
    nodes,
    dependency: null,
    selfIndex: entries.findIndex(([, number]) => number === self),
    trust: "tolerated",
    tolerances: [],
  } as StackParse;
}

const linear = (count: number) =>
  Array.from({ length: count }, (_, index) => [index, 100 + index] as [number, number]);
const numbersOf = (urls: string[]) => urls.map((url) => Number(url.split("/").pop()));

describe("stackWarmupOrder", () => {
  it("reads the PR below first, then the one on top, then outward", () => {
    expect(numbersOf(stackWarmupOrder(parseOf(linear(7), 103)))).toEqual([
      102, 104, 101, 105, 100, 106,
    ]);
  });

  it("reads every PR stacked right on this one before farther ones", () => {
    const branched = parseOf(
      [
        [0, 100],
        [1, 101],
        [2, 102],
        [2, 103],
        [3, 104],
      ],
      101,
    );
    expect(numbersOf(stackWarmupOrder(branched))).toEqual([100, 102, 103, 104]);
  });

  it("reads a long stack whole, up to the stack reads' limit, never this PR or an outside one", () => {
    const long = parseOf(linear(60), 120);
    const order = numbersOf(stackWarmupOrder(long));
    expect(order).toHaveLength(49);
    expect(order).not.toContain(120);
    expect(order.slice(0, 2)).toEqual([119, 121]);
    const outside = parseOf(linear(3), 101);
    outside.nodes[2] = { ...outside.nodes[2]!, external: true };
    expect(numbersOf(stackWarmupOrder(outside))).toEqual([100]);
  });
});

/** A clock, idle moments and a budget the test moves by hand. */
function fakePacing() {
  let now = 0;
  const timers: { at: number; work: () => void }[] = [];
  const idle: (() => void)[] = [];
  const eased = new Set<() => void>();
  const state = {
    budget: { pace: "normal" } as GitHubBudgetPace,
    cooldownUntil: null as number | null,
  };
  const pacing: StackWarmupPacing = {
    budget: () => state.budget,
    cooldownUntil: () => state.cooldownUntil,
    onBudgetEased: (listener) => {
      eased.add(listener);
      return () => eased.delete(listener);
    },
    now: () => now,
    whenIdle: (work) => {
      idle.push(work);
      return () => {
        const index = idle.indexOf(work);
        if (index >= 0) idle.splice(index, 1);
      };
    },
    setTimer: (work, ms) => {
      const timer = { at: now + ms, work };
      timers.push(timer);
      return () => {
        const index = timers.indexOf(timer);
        if (index >= 0) timers.splice(index, 1);
      };
    },
  };
  const runIdle = () => {
    while (idle.length) idle.shift()!();
  };
  /** Move the clock on, running each timer as it comes due and idle work after each. */
  const advance = async (ms: number): Promise<void> => {
    const end = now + ms;
    runIdle();
    await flush();
    const due = timers.filter((timer) => timer.at <= end).sort((a, b) => a.at - b.at)[0];
    if (due) {
      now = due.at;
      timers.splice(timers.indexOf(due), 1);
      due.work();
      return advance(end - now);
    }
    now = end;
    runIdle();
    await flush();
  };
  return {
    pacing,
    state,
    advance,
    idleWaiting: () => idle.length,
    ease: () => [...eased].forEach((listener) => listener()),
  };
}

const flush = () =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

/** Run `step` for each item, one after another, as a reader would. */
const inSequence = <Item>(items: Item[], step: (item: Item) => Promise<unknown>) =>
  items.reduce<Promise<unknown>>((done, item) => done.then(() => step(item)), Promise.resolve());

/** Every request the reads make, with when it started, each answered when the test says. */
function requests(clock: () => number) {
  const sent: { name: string; at: number; answer: () => void }[] = [];
  /** A read `name` that makes one request, then lets `next` go ahead. */
  const read = (
    name: string,
    {
      phase = 0,
      rank = 0,
      next = [] as StackWarmupRead[],
      settled = false,
      member = undefined as string | undefined,
    } = {},
  ): StackWarmupRead => ({
    phase,
    rank,
    member,
    settled: () => settled,
    run: () =>
      new Promise((resolve) => {
        sent.push({ name, at: clock(), answer: () => resolve(next) });
      }),
  });
  const answer = async (name: string) => {
    sent.find((each) => each.name === name)!.answer();
    await flush();
  };
  return { sent, read, answer, names: () => sent.map(({ name }) => name) };
}

describe("a reader's read-ahead queue", () => {
  it("sends at most two requests at once, each at least half a second after the last", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    queue.submit(
      {},
      ["a", "b", "c"].map((name) => net.read(name)),
    );
    await fake.advance(10_000);
    expect(net.sent.map(({ name, at }) => [name, at])).toEqual([
      ["a", 0],
      ["b", STACK_WARMUP_GAP_MS],
    ]);
    await net.answer("a");
    await fake.advance(0);
    expect(net.names()).toEqual(["a", "b", "c"]);
  });

  it("paces a read's follow-ups like any request, and lets them go before later phases", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const stack = net.read("a-stack");
    queue.submit({}, [
      net.read("a-details", { next: [stack] }),
      net.read("b-details", { rank: 1 }),
      net.read("c-changes", { phase: 1 }),
    ]);
    await fake.advance(0);
    await net.answer("a-details");
    await fake.advance(10_000);
    // A's stack, nearer, goes before B's details; changes, a later phase, after both.
    expect(net.sent.map(({ name, at }) => [name, at])).toEqual([
      ["a-details", 0],
      ["a-stack", 500],
      ["b-details", 1000],
    ]);
  });

  it("sends one request at a time, two seconds apart, while the budget runs low", async () => {
    const fake = fakePacing();
    fake.state.budget = { pace: "slow", factor: 4 };
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const follow = net.read("a-stack");
    queue.submit({}, [net.read("a", { next: [follow] }), net.read("b")]);
    await fake.advance(10_000);
    expect(net.names()).toEqual(["a"]);
    await net.answer("a");
    await fake.advance(0);
    await net.answer(net.names()[1]!);
    await fake.advance(10_000);
    const starts = net.sent.map(({ at }) => at);
    expect(starts).toHaveLength(3);
    for (let index = 1; index < starts.length; index += 1) {
      expect(starts[index]! - starts[index - 1]!).toBeGreaterThanOrEqual(STACK_WARMUP_SLOW_GAP_MS);
    }
  });

  it("holds a follow-up whose budget ran out while its details were on their way", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    queue.submit({}, [net.read("a-details", { next: [net.read("a-stack")] })]);
    await fake.advance(0);
    fake.state.budget = { pace: "stopped", until: 60_000 };
    await net.answer("a-details");
    await fake.advance(59_000);
    expect(net.names()).toEqual(["a-details"]);
    fake.state.budget = { pace: "normal" };
    await fake.advance(1_000);
    expect(net.names()).toEqual(["a-details", "a-stack"]);
  });

  it("asks the pace again when the idle moment comes, not only before", async () => {
    await inSequence(
      [
        (state: ReturnType<typeof fakePacing>["state"]) => {
          state.budget = { pace: "stopped", until: 60_000 };
        },
        (state: ReturnType<typeof fakePacing>["state"]) => {
          state.cooldownUntil = 60_000;
        },
      ],
      async (change) => {
        const fake = fakePacing();
        const queue = createStackWarmupQueue(fake.pacing);
        const net = requests(fake.pacing.now);
        queue.submit({}, [net.read("a")]);
        expect(fake.idleWaiting()).toBe(1);
        change(fake.state);
        await fake.advance(59_000);
        expect(net.names()).toEqual([]);
        fake.state.budget = { pace: "normal" };
        fake.state.cooldownUntil = null;
        await fake.advance(1_000);
        expect(net.names()).toEqual(["a"]);
      },
    );
  });

  it("goes on at once when the budget eases before its reset, as when pacing turns off", async () => {
    const fake = fakePacing();
    fake.state.budget = { pace: "stopped", until: 3_600_000 };
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    queue.submit({}, [net.read("a")]);
    await fake.advance(10_000);
    expect(net.names()).toEqual([]);
    fake.state.budget = { pace: "normal" };
    fake.ease();
    await fake.advance(0);
    expect(net.names()).toEqual(["a"]);
  });

  it("reads a late member's changes once its details answer, after the others'", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const changes = (member: string) => [
      net.read(`${member}-files`, { phase: 1 }),
      net.read(`${member}-patch`, { phase: 1 }),
    ];
    queue.submit({}, [
      net.read("a-details", { next: changes("a") }),
      net.read("b-details", { rank: 1, next: changes("b") }),
    ]);
    await fake.advance(0);
    await net.answer("a-details");
    // B's details take long; A's changes go meanwhile.
    await fake.advance(500);
    await inSequence(["a-files", "a-patch"], async (name) => {
      await fake.advance(500);
      await net.answer(name);
    });
    expect(net.names()).toEqual(["a-details", "b-details", "a-files", "a-patch"]);
    await fake.advance(4_000);
    await net.answer("b-details");
    await fake.advance(10_000);
    expect(net.names().slice(-2).sort()).toEqual(["b-files", "b-patch"]);
  });

  it("drops what a released view asked for, even a follow-up of a read under way", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const owner = {};
    queue.submit(owner, [net.read("a-details", { next: [net.read("a-stack")] }), net.read("b")]);
    await fake.advance(0);
    queue.release(owner);
    await net.answer("a-details");
    await fake.advance(60_000);
    expect(net.names()).toEqual(["a-details"]);
  });

  it("keeps counting requests under way when another view takes over", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const first = {};
    queue.submit(
      first,
      ["a", "b", "x"].map((name) => net.read(name)),
    );
    await fake.advance(500);
    expect(net.names()).toEqual(["a", "b"]);
    // Moving to another member: a new view asks for the rest, both requests still under way.
    await fake.advance(100);
    queue.release(first);
    const second = {};
    queue.submit(second, [net.read("c")]);
    await fake.advance(10_000);
    expect(queue.inFlight()).toBe(2);
    expect(net.names()).toEqual(["a", "b"]);
    await net.answer("a");
    await fake.advance(0);
    expect(net.names()).toEqual(["a", "b", "c"]);
    expect(net.sent[2]!.at - net.sent[1]!.at).toBeGreaterThanOrEqual(STACK_WARMUP_GAP_MS);
  });

  it("starts again after a quick pause and resume without a request out of turn", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    await inSequence([0, 1, 2], async (round) => {
      const owner = {};
      queue.submit(
        owner,
        ["a", "b", "c", "d"].map((name) => net.read(`${name}${round}`)),
      );
      await fake.advance(100);
      queue.release(owner);
    });
    await fake.advance(10_000);
    const starts = net.sent.map(({ at }) => at);
    expect(Math.max(...starts.map((at) => starts.filter((other) => other === at).length))).toBe(1);
    for (let index = 1; index < starts.length; index += 1) {
      expect(starts[index]! - starts[index - 1]!).toBeGreaterThanOrEqual(STACK_WARMUP_GAP_MS);
    }
    expect(queue.inFlight()).toBeLessThanOrEqual(2);
  });

  it("makes no request for what's already fresh, whatever the pace", async () => {
    const fake = fakePacing();
    fake.state.budget = { pace: "stopped", until: 3_600_000 };
    const queue = createStackWarmupQueue(fake.pacing);
    const ran: string[] = [];
    queue.submit({}, [
      {
        phase: 0,
        rank: 0,
        settled: () => true,
        run: async () => {
          ran.push("fresh");
          return [];
        },
      },
    ]);
    await fake.advance(0);
    expect(ran).toEqual(["fresh"]);
    expect(queue.inFlight()).toBe(0);
  });

  it("goes on past a read that fails, with nothing it would have let go ahead", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    queue.submit({}, [
      {
        phase: 0,
        rank: 0,
        settled: () => false,
        run: () => Promise.reject(new Error("GitHub couldn't answer")),
      },
      net.read("b"),
    ]);
    await fake.advance(10_000);
    expect(net.names()).toEqual(["b"]);
  });
});

describe("moving a member of interest ahead", () => {
  /** Member `name`'s details, whose answer lets its files and patch go ahead, as a stack's are. */
  const memberReads = (net: ReturnType<typeof requests>, name: string, rank: number) =>
    net.read(`${name}-details`, {
      rank,
      member: name,
      next: [
        net.read(`${name}-files`, { phase: STACK_WARMUP_CHANGES_PHASE, rank, member: name }),
        net.read(`${name}-patch`, { phase: STACK_WARMUP_CHANGES_PHASE, rank, member: name }),
      ],
    });
  const stackOf = (net: ReturnType<typeof requests>, names: string[]) =>
    names.map((name, rank) => memberReads(net, name, rank));
  /** Answer every request as soon as it's sent, until the queue has nothing left. */
  const answerAll = async (
    fake: ReturnType<typeof fakePacing>,
    net: ReturnType<typeof requests>,
  ) => {
    const answered = new Set<string>();
    const step = async (left: number): Promise<void> => {
      if (left === 0) return;
      const fresh = net.sent.filter(({ name }) => !answered.has(name));
      fresh.forEach(({ name }) => answered.add(name));
      await inSequence(fresh, ({ name }) => net.answer(name));
      await fake.advance(STACK_WARMUP_GAP_MS);
      return step(left - 1);
    };
    await step(100);
  };

  it("reads its details, files and patch before the other members' details", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const owner = {};
    queue.submit(owner, stackOf(net, ["a", "b", "c", "d"]));
    queue.prioritize(owner, "d", STACK_WARMUP_CHANGES_PHASE);
    await answerAll(fake, net);
    // The second request ahead goes while d's details are on their way, as it always may.
    expect(net.names().slice(0, 4)).toEqual(["d-details", "a-details", "d-files", "d-patch"]);
    expect(net.names()).toHaveLength(12);
  });

  it("moves only its details ahead where members open on their Overview", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const owner = {};
    queue.submit(owner, stackOf(net, ["a", "b", "c"]));
    queue.prioritize(owner, "c", STACK_WARMUP_DETAILS_PHASE);
    await answerAll(fake, net);
    expect(net.names().slice(0, 4)).toEqual(["c-details", "a-details", "b-details", "a-files"]);
    expect(net.names().indexOf("c-patch")).toBeGreaterThan(net.names().indexOf("b-patch"));
  });

  it("sends the same requests, at the same pace, as without interest", async () => {
    const run = async (interest: boolean) => {
      const fake = fakePacing();
      const queue = createStackWarmupQueue(fake.pacing);
      const net = requests(fake.pacing.now);
      const owner = {};
      queue.submit(owner, stackOf(net, ["a", "b", "c"]));
      if (interest) queue.prioritize(owner, "c", STACK_WARMUP_CHANGES_PHASE);
      await answerAll(fake, net);
      const starts = net.sent.map(({ at }) => at);
      return { names: [...net.names()].sort(), starts };
    };
    const without = await run(false);
    const withInterest = await run(true);
    expect(withInterest.names).toEqual(without.names);
    expect(withInterest.starts).toEqual(without.starts);
    for (let index = 1; index < withInterest.starts.length; index++) {
      expect(withInterest.starts[index]! - withInterest.starts[index - 1]!).toBeGreaterThanOrEqual(
        STACK_WARMUP_GAP_MS,
      );
    }
  });

  it("puts a member back in its place once the interest ends, even for reads its details let go later", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const owner = {};
    queue.submit(owner, stackOf(net, ["a", "b", "c"]));
    const end = queue.prioritize(owner, "c", STACK_WARMUP_CHANGES_PHASE);
    await fake.advance(0);
    expect(net.names()).toEqual(["c-details"]);
    // The pointer moves on while c's details are still on their way.
    end();
    await net.answer("c-details");
    await answerAll(fake, net);
    expect(net.names().slice(0, 4)).toEqual(["c-details", "a-details", "b-details", "a-files"]);
    expect(net.names().indexOf("c-patch")).toBe(net.names().length - 1);
  });

  it("follows the newest interest, and an older one ending leaves it in place", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const owner = {};
    queue.submit(owner, stackOf(net, ["a", "b", "c", "d"]));
    const endC = queue.prioritize(owner, "c", STACK_WARMUP_CHANGES_PHASE);
    await fake.advance(0);
    queue.prioritize(owner, "d", STACK_WARMUP_CHANGES_PHASE);
    endC();
    await net.answer("c-details");
    await answerAll(fake, net);
    expect(net.names().slice(0, 4)).toEqual(["c-details", "d-details", "d-files", "d-patch"]);
  });

  it("goes back to an earlier member when the user returns to it", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const owner = {};
    queue.submit(owner, stackOf(net, ["a", "b", "c", "d"]));
    queue.prioritize(owner, "c", STACK_WARMUP_CHANGES_PHASE)();
    queue.prioritize(owner, "d", STACK_WARMUP_CHANGES_PHASE)();
    queue.prioritize(owner, "c", STACK_WARMUP_CHANGES_PHASE);
    await answerAll(fake, net);
    expect(net.names().slice(0, 4)).toEqual(["c-details", "a-details", "c-files", "c-patch"]);
  });

  it("waits out a cooldown and a spent budget, then reads the member of interest first", async () => {
    const fake = fakePacing();
    fake.state.cooldownUntil = 60_000;
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const owner = {};
    queue.submit(owner, stackOf(net, ["a", "b", "c"]));
    queue.prioritize(owner, "c", STACK_WARMUP_CHANGES_PHASE);
    await fake.advance(30_000);
    expect(net.names()).toEqual([]);
    fake.state.cooldownUntil = null;
    fake.state.budget = { pace: "stopped", until: 90_000 };
    await fake.advance(40_000);
    expect(net.names()).toEqual([]);
    fake.state.budget = { pace: "normal" };
    await fake.advance(30_000);
    expect(net.names()[0]).toBe("c-details");
  });

  it("goes one request at a time while the budget runs low, member of interest first", async () => {
    const fake = fakePacing();
    fake.state.budget = { pace: "slow", factor: 4 };
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const owner = {};
    queue.submit(owner, stackOf(net, ["a", "b"]));
    queue.prioritize(owner, "b", STACK_WARMUP_CHANGES_PHASE);
    await fake.advance(10_000);
    expect(net.names()).toEqual(["b-details"]);
    await net.answer("b-details");
    await fake.advance(STACK_WARMUP_SLOW_GAP_MS);
    expect(net.names()).toEqual(["b-details", "b-files"]);
    expect(queue.inFlight()).toBe(1);
  });

  it("asks nothing again for a member whose reads are already fresh", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const owner = {};
    queue.submit(owner, [
      net.read("a-details"),
      net.read("b-details", { rank: 1, member: "b", settled: true }),
    ]);
    queue.prioritize(owner, "b", STACK_WARMUP_CHANGES_PHASE);
    await fake.advance(0);
    // b's fresh read runs without a request; only a's counts.
    expect(queue.inFlight()).toBe(1);
  });

  it("forgets the interest when its view lets go, and ignores interest from a view gone", async () => {
    const fake = fakePacing();
    const queue = createStackWarmupQueue(fake.pacing);
    const net = requests(fake.pacing.now);
    const gone = {};
    queue.submit(gone, stackOf(net, ["x"]));
    queue.prioritize(gone, "x", STACK_WARMUP_CHANGES_PHASE);
    queue.release(gone);
    const owner = {};
    queue.submit(owner, stackOf(net, ["a", "b"]));
    // Interest from the view gone neither reads for it nor moves the new view's reads.
    queue.prioritize(gone, "b", STACK_WARMUP_CHANGES_PHASE);
    await answerAll(fake, net);
    expect(net.names().slice(0, 2)).toEqual(["a-details", "b-details"]);
    expect(net.names()).not.toContain("x-details");
  });
});
