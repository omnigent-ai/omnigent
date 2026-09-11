import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NativeNotebookSurface } from "./NativeNotebookSurface";

const fetchers = vi.hoisted(() => ({ document: vi.fn(), native: vi.fn() }));
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: fetchers.document,
  fetchWithBrowserSession: fetchers.native,
}));
vi.mock("./NotebookHistory", () => ({ NotebookHistory: () => <div>Saved versions</div> }));
vi.mock("./notebook-pane.mjs", () => ({ mountNotebookPane: () => ({ dispose() {} }) }));

async function advance(ms = 0) {
  await act(() => vi.advanceTimersByTimeAsync(ms));
}
function iframe() {
  return screen.getByTitle<HTMLIFrameElement>("JupyterLab notebook");
}
function lab(restored: Promise<void> = Promise.resolve()) {
  const app = {
    restored,
    shell: {
      mode: "multiple-document",
      collapseLeft: vi.fn(),
      collapseRight: vi.fn(),
      currentWidget: { sessionContext: { kernelDisplayStatus: "idle" } },
    },
  };
  Object.assign(iframe().contentWindow!, { jupyterapp: app });
  return app;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  fetchers.document.mockReset();
  fetchers.document.mockImplementation(async () => new Response('{"format":"ipynb"}'));
  fetchers.native
    .mockReset()
    .mockImplementation(
      async () => new Response('{"url":"/v1/sessions/test/docloop/jupyter/lab/tree/task.ipynb"}'),
    );
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("native notebook loading recovery", () => {
  it.each(["missing app", "pending restore"])(
    "bounds %s and retries only on request",
    async (state) => {
      render(<NativeNotebookSurface sessionId="test" active />);
      await advance();
      const first = iframe();
      if (state === "pending restore") lab(new Promise(() => {}));
      await advance(120_000);
      expect(screen.getByRole("alert")).toHaveTextContent("did not finish loading");
      await advance(120_000);
      expect(fetchers.native).toHaveBeenCalledTimes(1);
      expect(iframe()).toBe(first);
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await advance();
      expect(iframe()).not.toBe(first);
      lab();
      await advance(500);
      expect(screen.queryByRole("alert")).toBeNull();
      expect(screen.getByRole("status")).toHaveTextContent("Kernel: idle");
      expect(fetchers.native).toHaveBeenCalledTimes(2);
    },
  );

  it("handles restoration rejection and recovers on retry", async () => {
    render(<NativeNotebookSurface sessionId="test" active />);
    await advance();
    let reject!: (reason: Error) => void;
    lab(
      new Promise((_, fail) => {
        reject = fail;
      }),
    );
    await advance(250);
    await act(async () => reject(new Error("private upstream details")));
    expect(screen.getByRole("alert")).toHaveTextContent("could not restore");
    expect(screen.queryByText(/private upstream/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await advance();
    lab();
    await advance(500);
    expect(screen.getByRole("status")).toHaveTextContent("Kernel: idle");
  });

  it("ignores a late restore from a timed-out iframe after retry", async () => {
    render(<NativeNotebookSurface sessionId="test" active />);
    await advance();
    let finish!: () => void;
    lab(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    await advance(120_000);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await advance();
    await act(async () => finish());
    expect(screen.getByRole("status")).toHaveTextContent("Loading JupyterLab");
    await advance(120_000);
    expect(screen.getByRole("alert")).toHaveTextContent("did not finish loading");
  });

  it("offers retry when same-origin access is lost", async () => {
    render(<NativeNotebookSurface sessionId="test" active />);
    await advance();
    Object.defineProperty(iframe().contentWindow!, "jupyterapp", {
      get() {
        throw new DOMException("cross-origin", "SecurityError");
      },
    });
    await advance(250);
    expect(screen.getByRole("alert")).toHaveTextContent("no longer accessible");
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
  });

  it("does not reload a ready notebook across Chat, History, or Source switches", async () => {
    const view = render(<NativeNotebookSurface sessionId="test" active />);
    await advance();
    const first = iframe();
    const app = lab();
    await advance(500);
    view.rerender(<NativeNotebookSurface sessionId="test" active={false} />);
    view.rerender(<NativeNotebookSurface sessionId="test" active />);
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    fireEvent.click(screen.getByRole("button", { name: "Source" }));
    fireEvent.click(screen.getByRole("button", { name: "JupyterLab" }));
    await advance(240_000);
    expect(iframe()).toBe(first);
    expect((iframe().contentWindow as Window & { jupyterapp?: unknown }).jupyterapp).toBe(app);
    expect(fetchers.native).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("aborts a stalled descriptor and ignores its late result after retry", async () => {
    let resolve!: (response: Response) => void;
    fetchers.native.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    render(<NativeNotebookSurface sessionId="test" active />);
    await advance(120_000);
    expect(screen.getByRole("alert")).toHaveTextContent("Opening the notebook timed out");
    expect(fetchers.native.mock.calls[0][1].aborted).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await advance();
    const current = iframe();
    await act(async () =>
      resolve(new Response('{"url":"/v1/sessions/test/docloop/jupyter/lab/tree/old.ipynb"}')),
    );
    expect(iframe()).toBe(current);
    expect(iframe().src).not.toContain("old.ipynb");
  });
});
