import { createRef } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { InfiniteScrollSentinel } from "./InfiniteScrollSentinel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("limits automatic loads, allows manual loads, and resets on a scope change", async () => {
  let intersect: () => void = () => {};
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      constructor(callback: IntersectionObserverCallback) {
        intersect = () =>
          callback(
            [{ isIntersecting: true } as IntersectionObserverEntry],
            this as unknown as IntersectionObserver,
          );
      }
      observe() {}
      disconnect() {}
    },
  );
  const fetchMore = vi.fn().mockResolvedValue(undefined);
  const root = createRef<HTMLElement>();
  const props = { hasMore: true, isFetching: false, fetchMore, scrollRoot: root };
  const view = render(<InfiniteScrollSentinel {...props} scopeKey="mine" />);
  // Pages must settle in sequence to exercise the automatic-load budget.
  /* oxlint-disable no-await-in-loop */
  for (let count = 1; count <= 3; count += 1) {
    await act(async () => {
      intersect();
      intersect();
    });
    await waitFor(() => expect(fetchMore).toHaveBeenCalledTimes(count));
  }
  /* oxlint-enable no-await-in-loop */
  await act(async () => intersect());
  expect(fetchMore).toHaveBeenCalledTimes(3);
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  await waitFor(() => expect(fetchMore).toHaveBeenCalledTimes(4));
  view.rerender(<InfiniteScrollSentinel {...props} scopeKey="shared" />);
  await act(async () => intersect());
  expect(fetchMore).toHaveBeenCalledTimes(5);
});

it("requires a click when automatic loading is disabled", async () => {
  const observe = vi.fn();
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      observe = observe;
      disconnect() {}
    },
  );
  const fetchMore = vi.fn().mockResolvedValue(undefined);
  render(
    <InfiniteScrollSentinel
      hasMore
      isFetching={false}
      fetchMore={fetchMore}
      scrollRoot={createRef<HTMLElement>()}
      maxAutoLoads={0}
    />,
  );
  expect(observe).not.toHaveBeenCalled();
  expect(fetchMore).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  await waitFor(() => expect(fetchMore).toHaveBeenCalledTimes(1));
});
