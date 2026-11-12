import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { lazy, Suspense, useLayoutEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChunkLoadErrorBoundary } from "./ChunkLoadErrorBoundary";
import { RenderErrorBoundary } from "./RenderErrorBoundary";

const renderError = new Error("Component render failed");
const chunkError = new TypeError("Failed to fetch dynamically imported module: /assets/old.js");
const reload = vi.fn();

function suppressExpectedError(event: ErrorEvent) {
  if (event.error === renderError || event.error === chunkError) event.preventDefault();
}

beforeEach(() => {
  window.addEventListener("error", suppressExpectedError);
  sessionStorage.clear();
  reload.mockReset();
  vi.stubGlobal("location", { reload });
  vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  window.removeEventListener("error", suppressExpectedError);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("RenderErrorBoundary", () => {
  it("preserves healthy child state when the session changes", () => {
    function Draft() {
      const [text, setText] = useState("");
      return (
        <input aria-label="Draft" value={text} onChange={(event) => setText(event.target.value)} />
      );
    }
    const view = render(
      <RenderErrorBoundary name="pull requests" resetKey="session-a">
        <Draft />
      </RenderErrorBoundary>,
    );
    const input = screen.getByRole("textbox", { name: "Draft" });
    fireEvent.change(input, { target: { value: "Unsent work" } });

    view.rerender(
      <RenderErrorBoundary name="pull requests" resetKey="session-b">
        <Draft />
      </RenderErrorBoundary>,
    );

    expect(screen.getByRole("textbox", { name: "Draft" })).toBe(input);
    expect(input).toHaveValue("Unsent work");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();
  });

  it("contains a descendant render failure, logs its stack, and retries on demand", () => {
    let broken = true;
    function BrokenChild() {
      if (broken) throw renderError;
      return <p>Panel recovered</p>;
    }
    const view = render(
      <>
        <p>Chat stays mounted</p>
        <ChunkLoadErrorBoundary>
          <RenderErrorBoundary name="pull requests" resetKey="session-a">
            <BrokenChild />
          </RenderErrorBoundary>
        </ChunkLoadErrorBoundary>
      </>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Unable to display pull requests.");
    expect(screen.getByText("Chat stays mounted")).toBeInTheDocument();
    expect(
      vi
        .mocked(console.error)
        .mock.calls.some(
          (args) =>
            args.includes(renderError) && args.some((arg) => String(arg).includes("BrokenChild")),
        ),
    ).toBe(true);
    expect(reload).not.toHaveBeenCalled();

    broken = false;
    view.rerender(
      <>
        <p>Chat stays mounted</p>
        <ChunkLoadErrorBoundary>
          <RenderErrorBoundary name="pull requests" resetKey="session-a">
            <BrokenChild />
          </RenderErrorBoundary>
        </ChunkLoadErrorBoundary>
      </>,
    );
    expect(screen.queryByText("Panel recovered")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("Panel recovered")).toBeInTheDocument();
  });

  it("retries a failed child when the session changes", () => {
    function SessionPanel({ session }: { session: string }) {
      if (session === "session-a") throw renderError;
      return <p>Another session</p>;
    }
    const view = render(
      <RenderErrorBoundary name="pull requests" resetKey="session-a">
        <SessionPanel session="session-a" />
      </RenderErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    view.rerender(
      <RenderErrorBoundary name="pull requests" resetKey="session-b">
        <SessionPanel session="session-b" />
      </RenderErrorBoundary>,
    );

    expect(screen.getByText("Another session")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("contains failures from descendant lifecycle hooks", () => {
    function BrokenEffect() {
      useLayoutEffect(() => {
        throw renderError;
      }, []);
      return <p>Panel content</p>;
    }
    render(
      <RenderErrorBoundary name="pull requests">
        <BrokenEffect />
      </RenderErrorBoundary>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Unable to display pull requests.");
    expect(screen.queryByText("Panel content")).not.toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();
  });

  it("contains failures during a descendant state update", () => {
    function UpdatedPanel() {
      const [broken, setBroken] = useState(false);
      if (broken) throw renderError;
      return (
        <button type="button" onClick={() => setBroken(true)}>
          Update panel
        </button>
      );
    }
    render(
      <RenderErrorBoundary name="pull requests">
        <UpdatedPanel />
      </RenderErrorBoundary>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Update panel" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Unable to display pull requests.");
    expect(reload).not.toHaveBeenCalled();
  });

  it("lets missing lazy chunks reach the existing automatic reload boundary", async () => {
    const Panel = lazy(() => Promise.reject(chunkError));
    render(
      <ChunkLoadErrorBoundary>
        <RenderErrorBoundary name="pull requests">
          <Suspense fallback={null}>
            <Panel />
          </Suspense>
        </RenderErrorBoundary>
      </ChunkLoadErrorBoundary>,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to load this page");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(reload).toHaveBeenCalledOnce();
  });
});
