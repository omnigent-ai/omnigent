// Tests for the shadcn Sonner renderer and the legacy showToast wrapper.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { Toaster } from "./sonner";
import { showToast } from "./toast";

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(async () => {
  // Finish Sonner's exit animations before the DOM environment is torn down.
  await act(async () => {
    toast.dismiss();
    await vi.runOnlyPendingTimersAsync();
  });
  cleanup();
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe("Toaster", () => {
  it("renders nothing until a toast is shown", () => {
    render(<Toaster />);
    expect(document.querySelector("[data-sonner-toast]")).toBeNull();
  });

  it("shows compatibility content and dismisses on the close button", async () => {
    render(<Toaster />);
    act(() => showToast(<span>Hello there</span>, { duration: 0 }));

    const toastRoot = await screen.findByTestId("toast");
    expect(toastRoot).toHaveTextContent("Hello there");

    fireEvent.click(screen.getByRole("button", { name: "Close toast" }));
    await waitFor(() => expect(screen.queryByTestId("toast")).toBeNull());
  });

  it("renders Sonner descriptions and actions", async () => {
    render(<Toaster />);
    act(() => {
      const id = toast("Permission required", {
        description: "Choose whether to continue.",
        duration: Number.POSITIVE_INFINITY,
        action: {
          label: "Continue",
          onClick: () => toast.dismiss(id),
        },
      });
    });

    expect(await screen.findByText("Permission required")).toBeInTheDocument();
    expect(screen.getByText("Choose whether to continue.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(screen.queryByText("Permission required")).toBeNull());
  });

  it("keeps a persistent permission prompt expanded beneath later notifications", async () => {
    render(<Toaster />);
    act(() => {
      toast.custom(() => <button type="button">Allow copying</button>, {
        id: "clipboard-permission",
        testId: "permission-toast",
        dismissible: false,
        duration: Number.POSITIVE_INFINITY,
      });
    });
    const permission = await screen.findByTestId("permission-toast");
    await waitFor(() => expect(permission).toHaveAttribute("data-expanded", "true"));

    act(() => {
      toast("An ordinary notification", {
        testId: "ordinary-toast",
        duration: Number.POSITIVE_INFINITY,
      });
    });
    const notification = await screen.findByTestId("ordinary-toast");
    await waitFor(() => {
      expect(permission).toHaveAttribute("data-front", "false");
      expect(permission).toHaveAttribute("data-expanded", "true");
      expect(notification).toHaveAttribute("data-expanded", "true");
    });

    act(() => toast.dismiss("clipboard-permission"));
    await waitFor(() => expect(screen.queryByTestId("permission-toast")).toBeNull());
    await waitFor(() => expect(notification).toHaveAttribute("data-expanded", "false"));
  });

  it("does not force expansion for a non-dismissible transient notification", async () => {
    render(<Toaster />);
    act(() => {
      toast("Upload in progress", {
        testId: "transient-toast",
        dismissible: false,
        duration: 10_000,
      });
    });
    const notification = await screen.findByTestId("transient-toast");
    await waitFor(() => expect(notification).toHaveAttribute("data-mounted", "true"));
    expect(notification).toHaveAttribute("data-expanded", "false");
  });

  it("preserves explicitly expanded ordinary notifications", async () => {
    render(<Toaster expand />);
    act(() => toast("Expanded notification", { testId: "expanded-toast" }));
    const notification = await screen.findByTestId("expanded-toast");
    await waitFor(() => expect(notification).toHaveAttribute("data-expanded", "true"));
  });
});
