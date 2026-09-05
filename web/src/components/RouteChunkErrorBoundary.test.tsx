import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CHUNK_RELOAD_AT_STORAGE_KEY, LazyChunkLoadError } from "@/lib/lazyChunkRecovery";
import { RouteChunkErrorBoundary } from "./RouteChunkErrorBoundary";

function Bomb({ error }: { error: unknown }): never {
  throw error;
}

afterEach(() => {
  sessionStorage.clear();
  vi.restoreAllMocks();
});

describe("RouteChunkErrorBoundary", () => {
  it("renders children when nothing fails", () => {
    render(
      <RouteChunkErrorBoundary>
        <div>page content</div>
      </RouteChunkErrorBoundary>,
    );
    expect(screen.getByText("page content")).toBeInTheDocument();
  });

  it("offers a reload when a route chunk cannot be loaded", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    sessionStorage.setItem(CHUNK_RELOAD_AT_STORAGE_KEY, String(Date.now()));
    const reloadPage = vi.fn();
    render(
      <RouteChunkErrorBoundary reloadPage={reloadPage}>
        <Bomb error={new LazyChunkLoadError(new Error("chunk 404"))} />
      </RouteChunkErrorBoundary>,
    );
    expect(screen.getByText("This page failed to load")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(reloadPage).toHaveBeenCalledTimes(1);
    // The user-invoked reload re-arms auto-recovery for the fresh document.
    expect(sessionStorage.getItem(CHUNK_RELOAD_AT_STORAGE_KEY)).toBeNull();
  });

  it("rethrows errors that are not chunk-load failures", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() =>
      render(
        <RouteChunkErrorBoundary>
          <Bomb error={new Error("real rendering bug")} />
        </RouteChunkErrorBoundary>,
      ),
    ).toThrow("real rendering bug");
  });
});
