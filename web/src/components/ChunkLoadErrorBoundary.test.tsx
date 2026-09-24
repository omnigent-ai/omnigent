import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { lazy, StrictMode, Suspense } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChunkLoadErrorBoundary } from "./ChunkLoadErrorBoundary";

const reload = vi.fn();
const chunkError = new TypeError("Failed to fetch dynamically imported module: /assets/old.js");
const renderError = new Error("Application bug");

function suppressExpectedError(event: ErrorEvent) {
  if (event.error === chunkError || event.error === renderError) event.preventDefault();
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

function renderFailedImport() {
  const Page = lazy(() => Promise.reject(chunkError));
  return render(
    <StrictMode>
      <ChunkLoadErrorBoundary>
        <Suspense fallback={<p>Loading</p>}>
          <Page />
        </Suspense>
      </ChunkLoadErrorBoundary>
    </StrictMode>,
  );
}

describe("ChunkLoadErrorBoundary", () => {
  it("renders a successfully loaded page without refreshing", async () => {
    const Page = lazy(async () => ({ default: () => <p>Page loaded</p> }));
    render(
      <ChunkLoadErrorBoundary>
        <Suspense fallback={null}>
          <Page />
        </Suspense>
      </ChunkLoadErrorBoundary>,
    );
    expect(await screen.findByText("Page loaded")).toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();
  });

  it("refreshes once when a lazy import rejects", async () => {
    renderFailedImport();
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to load this page");
    expect(reload).toHaveBeenCalledOnce();
  });

  it("shows a manual retry if the reloaded page still cannot load its chunk", async () => {
    renderFailedImport();
    await screen.findByRole("alert");
    cleanup();
    renderFailedImport();
    await screen.findByRole("alert");
    expect(reload).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "Refresh page" }));
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it("offers manual recovery when storage cannot guard an automatic reload", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("Storage unavailable");
    });
    renderFailedImport();
    await screen.findByRole("alert");
    expect(reload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Refresh page" }));
    expect(reload).toHaveBeenCalledOnce();
  });

  it("does not hide ordinary rendering errors or refresh for them", () => {
    function BrokenPage(): never {
      throw renderError;
    }
    expect(() =>
      render(
        <ChunkLoadErrorBoundary>
          <BrokenPage />
        </ChunkLoadErrorBoundary>,
      ),
    ).toThrow(renderError);
    expect(reload).not.toHaveBeenCalled();
  });
});
