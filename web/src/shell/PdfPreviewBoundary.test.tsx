import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { lazy, Suspense, useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
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
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not preview this PDF");
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Still here" } });
    expect(screen.getByLabelText("Message")).toHaveValue("Still here");
  } finally {
    window.removeEventListener("error", suppressExpectedError);
  }
});
