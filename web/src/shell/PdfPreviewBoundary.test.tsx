import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { lazy, Suspense, useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { ChunkLoadErrorBoundary } from "@/components/ChunkLoadErrorBoundary";
import { PdfPreviewBoundary } from "./PdfPreviewBoundary";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("contains a rejected PDF import while the surrounding conversation remains interactive", async () => {
  const error = new DOMException("Worker construction blocked", "SecurityError");
  const Pdf = lazy(() => Promise.reject(error));
  const suppressExpectedError = (event: ErrorEvent) => {
    if (event.error === error) event.preventDefault();
  };
  window.addEventListener("error", suppressExpectedError);
  vi.spyOn(console, "error").mockImplementation(() => {});

  function Session() {
    const [draft, setDraft] = useState("");
    return (
      <>
        <label>
          Message
          <input value={draft} onChange={(event) => setDraft(event.target.value)} />
        </label>
        <PdfPreviewBoundary>
          <Suspense fallback={<p>Loading PDF</p>}>
            <Pdf />
          </Suspense>
        </PdfPreviewBoundary>
      </>
    );
  }

  try {
    render(<Session />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to render PDF.");
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Still here" } });
    expect(screen.getByLabelText("Message")).toHaveValue("Still here");
  } finally {
    window.removeEventListener("error", suppressExpectedError);
  }
});

it("retries a failed preview only once new content arrives", async () => {
  const error = new Error("PDF renderer failed");
  let shouldThrow = true;
  function Pdf() {
    if (shouldThrow) throw error;
    return <p>PDF rendered</p>;
  }
  const suppressExpectedError = (event: ErrorEvent) => {
    if (event.error === error) event.preventDefault();
  };
  window.addEventListener("error", suppressExpectedError);
  vi.spyOn(console, "error").mockImplementation(() => {});

  try {
    const { rerender } = render(
      <PdfPreviewBoundary resetKey="v1">
        <Pdf />
      </PdfPreviewBoundary>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to render PDF.");

    shouldThrow = false;
    rerender(
      <PdfPreviewBoundary resetKey="v1">
        <Pdf />
      </PdfPreviewBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    rerender(
      <PdfPreviewBoundary resetKey="v2">
        <Pdf />
      </PdfPreviewBoundary>,
    );
    expect(await screen.findByText("PDF rendered")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  } finally {
    window.removeEventListener("error", suppressExpectedError);
  }
});

it("lets a stale-chunk import failure reach the app-level refresh boundary", async () => {
  const chunkError = new TypeError(
    "Failed to fetch dynamically imported module: /assets/PdfViewer-old.js",
  );
  const Pdf = lazy(() => Promise.reject(chunkError));
  const suppressExpectedError = (event: ErrorEvent) => {
    if (event.error === chunkError) event.preventDefault();
  };
  const reload = vi.fn();
  window.addEventListener("error", suppressExpectedError);
  sessionStorage.clear();
  vi.stubGlobal("location", { reload });
  vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
  vi.spyOn(console, "error").mockImplementation(() => {});

  try {
    render(
      <ChunkLoadErrorBoundary>
        <PdfPreviewBoundary>
          <Suspense fallback={<p>Loading PDF</p>}>
            <Pdf />
          </Suspense>
        </PdfPreviewBoundary>
      </ChunkLoadErrorBoundary>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to load this page");
    expect(reload).toHaveBeenCalledOnce();
  } finally {
    window.removeEventListener("error", suppressExpectedError);
    vi.unstubAllGlobals();
    sessionStorage.clear();
  }
});
