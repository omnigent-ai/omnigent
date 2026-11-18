import { act, render, screen } from "@testing-library/react";
import { Suspense } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CHUNK_RETRY_MAX_MS, CHUNK_RETRY_MS, retryingChunk } from "./retryingChunk";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

it("tries a chunk that can't load again less often each time, and shows it once it loads", async () => {
  const chunk = { broken: true, loads: 0 };
  const loaded = retryingChunk<object>(async () => {
    chunk.loads += 1;
    if (chunk.broken) throw new TypeError("Failed to fetch dynamically imported module");
    return () => <p>Loaded</p>;
  });
  const changes = vi.fn();
  loaded.subscribe(changes);
  function Shown() {
    const Component = loaded.useComponent();
    return <Component />;
  }
  render(
    <Suspense fallback={null}>
      <Shown />
    </Suspense>,
  );
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(loaded.missing()).toBe(true);
  expect(changes).toHaveBeenCalledTimes(1);
  // Each failed try waits twice as long for the next, up to a cap.
  chunk.loads = 0;
  let waitMs = CHUNK_RETRY_MS;
  for (let tries = 1; tries <= 8; tries += 1) {
    // oxlint-disable-next-line no-await-in-loop -- one wait at a time
    await act(() => vi.advanceTimersByTimeAsync(waitMs - 1));
    expect(chunk.loads).toBe(tries - 1);
    // oxlint-disable-next-line no-await-in-loop -- one wait at a time
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(chunk.loads).toBe(tries);
    waitMs = Math.min(waitMs * 2, CHUNK_RETRY_MAX_MS);
  }
  expect(waitMs).toBe(CHUNK_RETRY_MAX_MS);
  expect(screen.queryByText("Loaded")).toBeNull();
  // It loads again: shown, and no further tries.
  chunk.broken = false;
  await act(() => vi.advanceTimersByTimeAsync(CHUNK_RETRY_MAX_MS));
  expect(loaded.missing()).toBe(false);
  expect(changes).toHaveBeenCalledTimes(2);
  expect(screen.getByText("Loaded")).toBeTruthy();
  const loads = chunk.loads;
  await act(() => vi.advanceTimersByTimeAsync(CHUNK_RETRY_MAX_MS * 2));
  expect(chunk.loads).toBe(loads);
});

it("tries again only while something shows the chunk", async () => {
  const chunk = { broken: true, loads: 0 };
  const loaded = retryingChunk<object>(async () => {
    chunk.loads += 1;
    if (chunk.broken) throw new TypeError("Failed to fetch dynamically imported module");
    return () => <p>Loaded</p>;
  });
  function Shown() {
    const Component = loaded.useComponent();
    return <Component />;
  }
  const view = render(
    <Suspense fallback={null}>
      <Shown />
    </Suspense>,
  );
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(chunk.loads).toBe(1);
  view.unmount();
  await act(() => vi.advanceTimersByTimeAsync(CHUNK_RETRY_MAX_MS * 4));
  expect(chunk.loads).toBe(1);
  // Shown again: the try that came due runs then.
  chunk.broken = false;
  render(
    <Suspense fallback={null}>
      <Shown />
    </Suspense>,
  );
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(chunk.loads).toBe(2);
  expect(screen.getByText("Loaded")).toBeTruthy();
});
