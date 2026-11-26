import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/projectTicketsApi";
import type { ProjectTicket, TicketSearchPage } from "@/lib/projectTicketsApi";
import { highlightWords, TicketSearchBox, TicketSearchResults } from "./ProjectTicketSearch";

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ project_tickets: true, project_ticket_search: true }),
}));
vi.mock("@/lib/projectTicketsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  searchTickets: vi.fn(),
}));

const stage = { id: "open", name: "Pending", instructions: "", color: null, icon: null };

function row(id: string, title: string, match: TicketSearchPage["data"][number]["match"]) {
  return { id, title, stage_id: "open", updated_at: 1_700_000_000, match } as ProjectTicket & {
    match: typeof match;
  };
}

function page(data: TicketSearchPage["data"], hasMore = false): TicketSearchPage {
  return {
    data,
    stages: [stage],
    has_more: hasMore,
    last_id: hasMore ? (data.at(-1)?.id ?? "cursor") : null,
    scanned: 100,
  } as TicketSearchPage;
}

function renderResults(query: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onClear = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TicketSearchResults
          projectId="project"
          query={query}
          selectedTicketId={null}
          onClear={onClear}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return onClear;
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("ticket search", () => {
  it("marks every search word, whatever its case", () => {
    const { container } = render(
      <p>{highlightWords("Login page: LOGIN fails (x+1)", "login x+1")}</p>,
    );
    expect([...container.querySelectorAll("mark")].map((mark) => mark.textContent)).toEqual([
      "Login",
      "LOGIN",
      "x+1",
    ]);
  });

  it("lists matches newest first with stage, snippet and a link to the ticket", async () => {
    vi.mocked(api.searchTickets).mockResolvedValue(
      page([
        row("newer", "Fix signup", { field: "comment", snippet: "same login crash" }),
        row("older", "Login slow", { field: "title", snippet: "" }),
      ]),
    );
    const onClear = renderResults("login");

    const results = await screen.findAllByTestId("ticket-search-result");
    expect(results.map((result) => result.getAttribute("href"))).toEqual([
      "/?ticket=newer",
      "/?ticket=older",
    ]);
    expect(results[0].textContent).toContain("Comment: same login crash");
    expect(results[0].textContent).toContain("Pending");
    expect(screen.getByText("2 tickets, most recently updated first")).toBeTruthy();
    expect(api.searchTickets).toHaveBeenCalledWith("project", "login", { after: undefined });

    fireEvent.click(screen.getByRole("button", { name: "Back to board" }));
    expect(onClear).toHaveBeenCalled();
  });

  it("keeps searching older tickets while a page found few matches", async () => {
    vi.mocked(api.searchTickets)
      .mockResolvedValueOnce(page([], true))
      .mockResolvedValueOnce(
        page([row("found", "Old login bug", { field: "title", snippet: "" })]),
      );
    renderResults("login");

    expect(await screen.findByTestId("ticket-search-result")).toBeTruthy();
    expect(api.searchTickets).toHaveBeenLastCalledWith("project", "login", { after: "cursor" });
  });

  it("says when nothing matches", async () => {
    vi.mocked(api.searchTickets).mockResolvedValue(page([]));
    renderResults("zebra");
    expect(await screen.findByText("No tickets match “zebra”")).toBeTruthy();
  });

  it("clears the box on Escape", () => {
    const onChange = vi.fn();
    render(<TicketSearchBox value="login" onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("searchbox", { name: "Search tickets" }), {
      key: "Escape",
    });
    expect(onChange).toHaveBeenCalledWith("");
  });
});
