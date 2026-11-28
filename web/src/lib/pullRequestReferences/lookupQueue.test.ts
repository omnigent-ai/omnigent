import { describe, expect, it, vi } from "vitest";

import { createLookupQueue, createPageLookupBudget, LookupHeldError } from "./lookupQueue";

/** A lookup that settles when told to. */
function deferred() {
  let settle!: () => void;
  const done = new Promise<string>((resolve) => {
    settle = () => resolve("done");
  });
  return { done, settle };
}

describe("createLookupQueue", () => {
  it("runs no more than its limit at once, and every lookup in the end", async () => {
    const queue = createLookupQueue(4);
    let running = 0;
    let most = 0;
    const lookups = Array.from({ length: 300 }, (_, index) =>
      queue.run(async () => {
        running += 1;
        most = Math.max(most, running);
        await new Promise((resolve) => {
          setTimeout(resolve, index % 3);
        });
        running -= 1;
        return index;
      }),
    );

    const results = await Promise.all(lookups);

    expect(most).toBe(4);
    expect(results).toHaveLength(300);
  });

  it("frees a slot when a lookup fails", async () => {
    const queue = createLookupQueue(1);
    await expect(queue.run(() => Promise.reject(new Error("no")))).rejects.toThrow("no");
    await expect(queue.run(() => Promise.resolve("next"))).resolves.toBe("next");
  });
});

describe("createLookupQueue's gates", () => {
  it("never sends a waiting lookup whose view went away, and gives its turn to the next", async () => {
    const queue = createLookupQueue(1);
    const first = deferred();
    const running = queue.run(() => first.done);
    const gone = new AbortController();
    const sent: string[] = [];
    const abandoned = queue.run(
      async () => {
        sent.push("abandoned");
        return "abandoned";
      },
      { signal: gone.signal },
    );
    const next = queue.run(async () => {
      sent.push("next");
      return "next";
    });

    gone.abort();
    first.settle();

    await expect(abandoned).rejects.toThrow();
    await expect(running).resolves.toBe("done");
    await expect(next).resolves.toBe("next");
    expect(sent).toEqual(["next"]);
  });

  it("asks again when a waiting lookup's turn comes, and doesn't send it while reads are held", async () => {
    const queue = createLookupQueue(1);
    const first = deferred();
    const running = queue.run(() => first.done);
    let allowed = true;
    const lookup = vi.fn(async () => "sent");
    const waiting = queue.run(lookup, { stillAllowed: () => allowed });

    allowed = false;
    first.settle();

    await running;
    await expect(waiting).rejects.toBeInstanceOf(LookupHeldError);
    expect(lookup).not.toHaveBeenCalled();
    // The held lookup gave its slot back.
    await expect(queue.run(async () => "after")).resolves.toBe("after");
  });
});

describe("createPageLookupBudget", () => {
  it("admits PRs up to its cap, and again any it already admitted", () => {
    const budget = createPageLookupBudget(2);
    expect([budget.admit("a", "a"), budget.admit("b", "b"), budget.admit("c", "c")]).toEqual([
      true,
      true,
      false,
    ]);
    expect(budget.admit("a", "a")).toBe(true);
  });
});
