import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { usePresentedRichPaths } from "./usePresentedRichPaths";

// Deferred updates wait here until a test releases them, so the pending interval is the test's.
const pending = vi.hoisted(() => [] as (() => void)[]);
vi.mock("./deferPresentation", () => ({
  deferPresentation: (apply: () => void) => pending.push(apply),
}));

function release() {
  act(() => {
    for (const apply of pending.splice(0)) apply();
  });
}

const NONE: ReadonlySet<string> = new Set();
const GUIDE: ReadonlySet<string> = new Set(["docs/guide.md"]);

function presented(scope: string, intended: ReadonlySet<string>) {
  return renderHook((props) => usePresentedRichPaths(props.scope, props.intended), {
    initialProps: { scope, intended },
  });
}

beforeEach(() => {
  pending.length = 0;
});

describe("usePresentedRichPaths", () => {
  it("shows a new choice only once the deferred update runs", () => {
    const view = presented("review-a", NONE);
    view.rerender({ scope: "review-a", intended: GUIDE });
    expect(view.result.current).toBe(NONE);
    expect(pending).toHaveLength(1);
    release();
    expect(view.result.current).toBe(GUIDE);
  });

  it("settles on the newest choice when the reader switches back before the first shows", () => {
    const view = presented("review-a", NONE);
    view.rerender({ scope: "review-a", intended: GUIDE });
    view.rerender({ scope: "review-a", intended: NONE });
    // Both updates run, oldest first: neither leaves the rich diff showing.
    release();
    expect(view.result.current).toBe(NONE);
  });

  it("lets a late update show only the newest choice", () => {
    const view = presented("review-a", NONE);
    view.rerender({ scope: "review-a", intended: GUIDE });
    const [first] = pending.splice(0);
    const other: ReadonlySet<string> = new Set(["docs/other.md"]);
    view.rerender({ scope: "review-a", intended: other });
    act(() => first!());
    expect(view.result.current).toBe(other);
  });

  it("shows another review's own choice at once, even for the same path", () => {
    const view = presented("review-a", GUIDE);
    view.rerender({ scope: "review-b", intended: NONE });
    expect(view.result.current).toBe(NONE);
    view.rerender({ scope: "review-a", intended: GUIDE });
    expect(view.result.current).toBe(GUIDE);
  });

  it("drops a change still pending when the review changes", () => {
    const view = presented("review-a", NONE);
    view.rerender({ scope: "review-a", intended: GUIDE });
    view.rerender({ scope: "review-b", intended: NONE });
    release();
    expect(view.result.current).toBe(NONE);
  });
});
