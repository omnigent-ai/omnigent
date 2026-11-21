import { act, cleanup, render } from "@testing-library/react";
import { useDeferredValue, useLayoutEffect } from "react";
import { flushSync } from "react-dom";
import { afterEach, describe, expect, it } from "vitest";

import { useValueAfterDeferredRender } from "./useValueAfterDeferredRender";

const commits: string[] = [];

function Probe({ value }: { value: string }) {
  const deferred = useDeferredValue(value);
  const later = useValueAfterDeferredRender(value);
  // Recorded on commit, so a render React abandons doesn't count.
  useLayoutEffect(() => {
    commits.push(`${value}/${deferred}/${later}`);
  });
  return null;
}

afterEach(() => {
  cleanup();
  commits.length = 0;
});

async function nextFrames() {
  await act(
    () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => resolve());
      }),
  );
}

describe("useValueAfterDeferredRender", () => {
  it("starts at the value it mounts with", () => {
    render(<Probe value="a" />);
    expect(commits).toEqual(["a/a/a"]);
  });

  it("takes a change only after the urgent and the deferred render, in one of its own", async () => {
    const view = render(<Probe value="a" />);
    commits.length = 0;
    act(() => {
      flushSync(() => view.rerender(<Probe value="b" />));
      expect(commits).toEqual(["b/a/a"]);
    });
    // The deferred render follows without it.
    expect(commits).toEqual(["b/a/a", "b/b/a"]);
    await nextFrames();
    expect(commits.at(-1)).toBe("b/b/b");
  });

  it("settles on the latest value when it changes back before taking the first", async () => {
    const view = render(<Probe value="a" />);
    view.rerender(<Probe value="b" />);
    view.rerender(<Probe value="a" />);
    await nextFrames();
    expect(commits.filter((commit) => commit.endsWith("/b"))).toEqual([]);
    expect(commits.at(-1)).toBe("a/a/a");
  });
});
