import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as ticketsApi from "@/lib/projectTicketsApi";
import { TicketHistorySummary } from "./TicketHistorySummary";

vi.mock("@/lib/projectTicketsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ticketsApi>()),
  getTicketHistorySummary: vi.fn(),
  summarizeTicketHistory: vi.fn(),
}));

const NOW = 1_800_000_000_000;
const made = (stale = false): ticketsApi.TicketHistorySummary => ({
  object: "project.ticket.summary",
  available: true,
  stale,
  summary: {
    text: "**What was asked**\n\nShip the dialog.",
    through_position: 24,
    generated_at: NOW / 1000 - 300,
    model: "judge",
    cached: false,
  },
});

function renderSummary() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <TicketHistorySummary projectId="p" ticketId="t" nowMs={NOW} />
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

describe("TicketHistorySummary", () => {
  it("offers nothing when the server has no model for summaries", async () => {
    vi.mocked(ticketsApi.getTicketHistorySummary).mockResolvedValue({
      object: "project.ticket.summary",
      available: false,
      summary: null,
      stale: false,
    });
    renderSummary();
    await vi.waitFor(() => expect(ticketsApi.getTicketHistorySummary).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Summarize" })).toBeNull();
  });

  it("generates a private summary on request and hides it again", async () => {
    vi.mocked(ticketsApi.getTicketHistorySummary).mockResolvedValue({
      ...made(),
      summary: null,
    });
    vi.mocked(ticketsApi.summarizeTicketHistory).mockResolvedValue(made());
    renderSummary();
    await userEvent.click(await screen.findByRole("button", { name: "Summarize" }));
    const region = await screen.findByRole("region", { name: "Ticket summary" });
    expect(region.textContent).toContain("Ship the dialog.");
    expect(region.textContent).toContain("Not posted to the ticket · 5m ago");
    expect(ticketsApi.summarizeTicketHistory).toHaveBeenCalledWith("p", "t");
    await userEvent.click(screen.getByRole("button", { name: "Hide summary" }));
    // A fresh summary reopens without another model call.
    await userEvent.click(screen.getByRole("button", { name: "Summarize" }));
    expect(ticketsApi.summarizeTicketHistory).toHaveBeenCalledTimes(1);
  });

  it("offers to summarize again after newer activity", async () => {
    vi.mocked(ticketsApi.getTicketHistorySummary).mockResolvedValue(made(true));
    vi.mocked(ticketsApi.summarizeTicketHistory).mockResolvedValue(made());
    renderSummary();
    await userEvent.click(await screen.findByRole("button", { name: "Summarize again" }));
    expect(ticketsApi.summarizeTicketHistory).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("region", { name: "Ticket summary" })).toBeTruthy();
  });
});
