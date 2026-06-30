// Unit tests for the minimal toast system: showToast() renders content into a
// mounted <Toaster />, and the dismiss control removes it.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { showToast, Toaster } from "./toast";

afterEach(cleanup);

describe("Toaster", () => {
  it("renders nothing until a toast is shown", () => {
    render(<Toaster />);
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  it("shows toast content and dismisses on the close button", () => {
    render(<Toaster />);
    act(() => showToast(<span>Hello there</span>, { duration: 0 }));

    const toast = screen.getByTestId("toast");
    expect(toast).toHaveTextContent("Hello there");

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("toast")).toBeNull();
  });
});
