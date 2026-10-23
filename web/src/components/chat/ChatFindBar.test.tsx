import { act, fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatFindBar } from "./ChatFindBar";

const searchSessionItems = vi.hoisted(() => vi.fn());
vi.mock("@/lib/sessionsApi", () => ({ searchSessionItems }));

const matches = [
  { id: "item_a", response_id: "resp_a" },
  { id: "item_b", response_id: "resp_b" },
  { id: "item_c", response_id: "resp_c" },
];

function bar(start: "newest" | "oldest", onTarget = vi.fn(), query = "zorblat") {
  return (
    <ChatFindBar
      conversationId="conv_1"
      query={query}
      onQueryChange={() => {}}
      start={start}
      inputRef={createRef<HTMLInputElement>()}
      onTarget={onTarget}
      onClose={() => {}}
    />
  );
}

async function flush(ms = 300) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function renderBar(start: "newest" | "oldest") {
  const onTarget = vi.fn();
  const view = render(bar(start, onTarget));
  await flush();
  return { onTarget, view };
}

/** The newest window holds item_b and item_c; the older one stays in flight until `resolve`. */
function twoPages() {
  let resolve: (page: unknown) => void = () => {};
  searchSessionItems.mockImplementation((_id: string, _q: string, { before }) =>
    before === undefined
      ? Promise.resolve({ matches: matches.slice(1), nextBefore: 4 })
      : new Promise((done) => {
          resolve = done;
        }),
  );
  return {
    finish: async () => {
      resolve({ matches: matches.slice(0, 1), nextBefore: null });
      await flush(0);
    },
  };
}

describe("ChatFindBar", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    searchSessionItems.mockReset();
    searchSessionItems.mockResolvedValue({ matches, nextBefore: null });
  });
  afterEach(() => vi.useRealTimers());

  it("starts ⌘F at the newest match and steps through every match with the counter", async () => {
    const { onTarget } = await renderBar("newest");
    expect(searchSessionItems).toHaveBeenCalledWith("conv_1", "zorblat", expect.anything());
    expect(screen.getByText("3 / 3")).toBeTruthy();
    expect(onTarget).toHaveBeenLastCalledWith(
      expect.objectContaining({ candidates: ["item_c", "resp_c"] }),
    );

    const input = screen.getByRole("textbox", { name: "Find in chat" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByText("1 / 3")).toBeTruthy();
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(screen.getByText("3 / 3")).toBeTruthy();
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(screen.getByText("2 / 3")).toBeTruthy();
    expect(onTarget).toHaveBeenLastCalledWith(
      expect.objectContaining({ candidates: ["item_b", "resp_b"] }),
    );
  });

  it("starts a search result at the oldest match", async () => {
    const { onTarget } = await renderBar("oldest");
    expect(screen.getByText("1 / 3")).toBeTruthy();
    expect(onTarget).toHaveBeenLastCalledWith(
      expect.objectContaining({ candidates: ["item_a", "resp_a"] }),
    );
  });

  it("jumps ⌘F to the newest match from the first page and counts older pages as they land", async () => {
    const pages = twoPages();
    const { onTarget } = await renderBar("newest");
    expect(searchSessionItems).toHaveBeenLastCalledWith(
      "conv_1",
      "zorblat",
      expect.objectContaining({ before: 4 }),
    );
    expect(screen.getByText("2 / 2+")).toBeTruthy();
    expect(onTarget).toHaveBeenLastCalledWith(
      expect.objectContaining({ candidates: ["item_c", "resp_c"] }),
    );
    const jumps = onTarget.mock.calls.length;

    // Older matches are not loaded yet, so Next does not wrap around.
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Find in chat" }), { key: "Enter" });
    expect(screen.getByText("2 / 2+")).toBeTruthy();

    await pages.finish();
    expect(screen.getByText("3 / 3")).toBeTruthy();
    expect(onTarget).toHaveBeenCalledTimes(jumps);
  });

  it("waits for the last page before starting a search result at the oldest match", async () => {
    const pages = twoPages();
    const { onTarget } = await renderBar("oldest");
    expect(screen.getByText("Searching…")).toBeTruthy();
    expect(onTarget).not.toHaveBeenCalledWith(expect.objectContaining({ key: expect.anything() }));

    await pages.finish();
    expect(screen.getByText("1 / 3")).toBeTruthy();
    expect(onTarget).toHaveBeenLastCalledWith(
      expect.objectContaining({ candidates: ["item_a", "resp_a"] }),
    );
  });

  it("aborts the page walk when the query changes or the bar closes", async () => {
    twoPages();
    const { view } = await renderBar("newest");
    const firstWalk = searchSessionItems.mock.lastCall?.[2].signal as AbortSignal;
    expect(firstWalk.aborted).toBe(false);

    view.rerender(bar("newest", vi.fn(), "quorvantix"));
    expect(firstWalk.aborted).toBe(true);

    await flush();
    const secondWalk = searchSessionItems.mock.lastCall?.[2].signal as AbortSignal;
    view.unmount();
    expect(secondWalk.aborted).toBe(true);
  });

  it("says when a query has no match in any page", async () => {
    searchSessionItems.mockResolvedValue({ matches: [], nextBefore: null });
    await renderBar("newest");
    expect(screen.getByText("No results")).toBeTruthy();
  });
});
