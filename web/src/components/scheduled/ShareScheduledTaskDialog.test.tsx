import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import * as api from "@/lib/scheduledTasksApi";
import { ShareScheduledTaskDialog } from "./ShareScheduledTaskDialog";

vi.mock("@/lib/scheduledTasksApi", () => ({
  listScheduledTaskRecipients: vi.fn(),
  shareScheduledTask: vi.fn(),
  unshareScheduledTask: vi.fn(),
}));
beforeEach(() => {
  vi.mocked(api.listScheduledTaskRecipients).mockResolvedValue(["bob@example.com"]);
  vi.mocked(api.shareScheduledTask).mockResolvedValue();
  vi.mocked(api.unshareScheduledTask).mockResolvedValue();
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
function showDialog(sharingOff = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CapabilitiesProvider
        info={{ ...SERVER_INFO_OFFLINE_FALLBACK, sharing_mode: sharingOff ? "off" : "on" }}
      >
        <ShareScheduledTaskDialog task={{ id: "task", name: "Triage" }} onClose={vi.fn()} />
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}
it("shares with a selected person and lets the owner revoke access", async () => {
  showDialog();
  expect(await screen.findByText("bob@example.com")).toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "Person" }), {
    target: { value: "carol@example.com" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Share" }));
  await waitFor(() =>
    expect(api.shareScheduledTask).toHaveBeenCalledWith("task", "carol@example.com"),
  );
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Person" })).toHaveValue(""));
  fireEvent.click(screen.getByRole("button", { name: "Remove bob@example.com" }));
  await waitFor(() =>
    expect(api.unshareScheduledTask).toHaveBeenCalledWith("task", "bob@example.com"),
  );
});
it("allows revocation when deployment policy disables new shares", async () => {
  showDialog(true);
  expect(screen.getByRole("button", { name: "Share" })).toBeDisabled();
  fireEvent.click(await screen.findByRole("button", { name: "Remove bob@example.com" }));
  await waitFor(() =>
    expect(api.unshareScheduledTask).toHaveBeenCalledWith("task", "bob@example.com"),
  );
  expect(api.shareScheduledTask).not.toHaveBeenCalled();
});
