import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Toaster } from "sonner";

import { beginClone, detachClone, pendingClone } from "./cloneRequests";

afterEach(cleanup);

it("forgets a clone when the user dismisses its loading toast", async () => {
  render(<Toaster />);
  const request = beginClone("conv_src:", { onSuccess: vi.fn(), onError: vi.fn() }, vi.fn());
  detachClone(request);
  await screen.findByText("Creating session…");

  fireEvent.click(screen.getByRole("button", { name: "Close toast" }));

  await waitFor(() => expect(pendingClone("conv_src:")).toBeUndefined());
});
