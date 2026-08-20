import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Toaster } from "@/components/ui/toast";
import { showBulkTeardownFailureToast, showTeardownFailureToast } from "./WorktreeTeardownToast";

// The teardown script runs after the session row is already gone, so a toast
// is the ONLY place its failure can surface. These pin that the reason reaches
// the DOM and that the captured output is reachable behind the disclosure.

afterEach(cleanup);

describe("showTeardownFailureToast", () => {
  it("names the session and reason, and keeps the output expandable", () => {
    render(<Toaster />);
    act(() =>
      showTeardownFailureToast({
        label: "Fix login",
        reason: "exited with status 1",
        outputTail: "could not stop dev server",
      }),
    );
    expect(
      screen.getByText(/Teardown script for Fix login exited with status 1/),
    ).toBeInTheDocument();
    // The session still went away — the toast must not read as a failed delete.
    expect(screen.getByText(/still deleted/)).toBeInTheDocument();
    // Output is collapsed behind a disclosure so a 10 KB tail can't fill the
    // screen, but it is present.
    const details = screen.getByTestId("teardown-toast-output");
    fireEvent.click(screen.getByText("View output"));
    expect(details).toHaveTextContent("could not stop dev server");
  });

  it("omits the disclosure when the script printed nothing", () => {
    render(<Toaster />);
    act(() => showTeardownFailureToast({ label: "Quiet", reason: "timed out", outputTail: "" }));
    expect(screen.getByText(/Teardown script for Quiet timed out/)).toBeInTheDocument();
    expect(screen.queryByTestId("teardown-toast-output")).not.toBeInTheDocument();
  });
});

describe("showBulkTeardownFailureToast", () => {
  it("collapses several failures into one counted line", () => {
    // WHY: a bulk delete runs one script per worktree; N toasts would bury
    // the sidebar.
    render(<Toaster />);
    act(() =>
      showBulkTeardownFailureToast(
        [
          { label: "A", reason: "timed out", outputTail: "a-output" },
          { label: "B", reason: "exited with status 2", outputTail: "b-output" },
        ],
        5,
      ),
    );
    expect(screen.getByText(/2 of 5 teardown scripts failed/)).toBeInTheDocument();
    const details = screen.getByTestId("teardown-toast-output");
    // Each failure's output is labeled so the user can tell them apart.
    expect(details).toHaveTextContent("a-output");
    expect(details).toHaveTextContent("b-output");
  });

  it("renders nothing when every teardown succeeded", () => {
    render(<Toaster />);
    act(() => showBulkTeardownFailureToast([], 3));
    expect(screen.queryByTestId("teardown-toast")).not.toBeInTheDocument();
  });
});
