import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { copyText } from "@/lib/clipboard";
import { TicketCopyLinkButton } from "./TicketCopyLinkButton";

vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));

const ticketUrl = `${window.location.origin}/projects/Demo%20board?ticket=tkt_1`;

function renderButton() {
  return render(
    <TooltipProvider>
      <TicketCopyLinkButton projectName="Demo board" ticketId="tkt_1" />
    </TooltipProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("TicketCopyLinkButton", () => {
  it("copies the ticket URL and confirms", async () => {
    vi.mocked(copyText).mockResolvedValue();
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: "Copy URL" }));

    expect(copyText).toHaveBeenCalledWith(ticketUrl);
    expect(await screen.findByRole("button", { name: "Copied" })).toBeTruthy();
  });

  it("offers the URL to copy by hand when the clipboard is unavailable", async () => {
    vi.mocked(copyText).mockRejectedValue(new Error("Clipboard API not available"));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: "Copy URL" }));

    expect((await screen.findByTestId("ticket-copy-link-fallback")).textContent).toBe(ticketUrl);
  });
});
