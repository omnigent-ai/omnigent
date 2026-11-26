import { act, render } from "@testing-library/react";
import { Suspense, useLayoutEffect, useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import { describe, expect, it } from "vitest";
import { usePresentedRichPaths } from "./usePresentedRichPaths";

// The real transition, not a delayed callback: React itself holds the rich render here.
const GUIDE = "docs/guide.md";
const NONE: ReadonlySet<string> = new Set();
const RICH: ReadonlySet<string> = new Set([GUIDE]);

/** The reader's stored choice, read as the review reads it. */
function choiceStore() {
  let value = NONE;
  const listeners = new Set<() => void>();
  return {
    get: () => value,
    set(next: ReadonlySet<string>) {
      value = next;
      for (const listener of listeners) listener();
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** Rendering the rich view waits until `release`, as a slow rich render would. */
function richGate() {
  let open = false;
  let resolve = () => {};
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return {
    hold() {
      if (!open) throw promise;
    },
    release() {
      open = true;
      resolve();
    },
  };
}

function Review({
  store,
  gate,
  commits,
  scope = "review",
}: {
  store: ReturnType<typeof choiceStore>;
  gate: ReturnType<typeof richGate>;
  commits: string[];
  scope?: string;
}) {
  const intended = useSyncExternalStore(store.subscribe, store.get);
  const shown = usePresentedRichPaths(scope, intended);
  const rich = shown.has(GUIDE);
  useLayoutEffect(() => {
    commits.push(rich ? "rich" : "source");
  }, [commits, rich]);
  return rich ? <RichView gate={gate} /> : <p>source</p>;
}

function RichView({ gate }: { gate: ReturnType<typeof richGate> }) {
  gate.hold();
  return <p>rich</p>;
}

describe("usePresentedRichPaths with React's own transitions", () => {
  it("never shows a rich choice the reader withdrew while it was still rendering", async () => {
    const store = choiceStore();
    const gate = richGate();
    const commits: string[] = [];
    render(
      <Suspense fallback={<p>fallback</p>}>
        <Review store={store} gate={gate} commits={commits} />
      </Suspense>,
    );
    expect(commits).toEqual(["source"]);
    // The reader asks for the rich diff; its render is held.
    await act(async () => store.set(RICH));
    // Then withdraws it before that render commits, by a second click or a line request.
    act(() => flushSync(() => store.set(NONE)));
    const afterWithdrawal = commits.length;
    await act(async () => {
      gate.release();
      await Promise.resolve();
    });
    expect(commits.slice(afterWithdrawal)).not.toContain("rich");
    expect(commits.at(-1)).toBe("source");
  });

  it("shows a rich choice the reader kept once its render is done", async () => {
    const store = choiceStore();
    const gate = richGate();
    const commits: string[] = [];
    render(
      <Suspense fallback={<p>fallback</p>}>
        <Review store={store} gate={gate} commits={commits} />
      </Suspense>,
    );
    await act(async () => store.set(RICH));
    expect(commits).toEqual(["source"]);
    await act(async () => {
      gate.release();
      await Promise.resolve();
    });
    expect(commits).toEqual(["source", "rich"]);
  });

  it("never shows the last review's rich choice in the next one, for the same path", async () => {
    const first = choiceStore();
    const second = choiceStore();
    const gate = richGate();
    const commits: string[] = [];
    const view = (scope: string, store: ReturnType<typeof choiceStore>) => (
      <Suspense fallback={<p>fallback</p>}>
        <Review scope={scope} store={store} gate={gate} commits={commits} />
      </Suspense>
    );
    const rendered = render(view("review-a", first));
    // Review A's rich choice is still rendering when the reader opens review B.
    await act(async () => first.set(RICH));
    act(() => flushSync(() => rendered.rerender(view("review-b", second))));
    const afterSwitch = commits.length;
    await act(async () => {
      gate.release();
      await Promise.resolve();
    });
    expect(commits.slice(afterSwitch)).not.toContain("rich");
  });
});
