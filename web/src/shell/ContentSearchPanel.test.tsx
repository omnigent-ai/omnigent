import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { ContentSearchPanel } from "./ContentSearchPanel";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

it("searches only on submission and allows the same query to run again", async () => {
  vi.mocked(authenticatedFetch).mockImplementation(
    async () => new Response(JSON.stringify({ matches: [], truncated: false, warning: null })),
  );
  const client = new QueryClient();
  const view = (request: number) => (
    <StrictMode>
      <QueryClientProvider client={client}>
        <ContentSearchPanel key={request} conversationId="session-one" onFileSelect={vi.fn()} />
      </QueryClientProvider>
    </StrictMode>
  );
  const { rerender } = render(view(1));
  expect(authenticatedFetch).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "  needle  " } });
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  expect(await screen.findByText(/No matches for/)).toBeInTheDocument();
  expect(screen.getByRole("searchbox")).toHaveValue("  needle  ");
  expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(2));
  expect(await screen.findByText(/No matches for/)).toBeInTheDocument();
  rerender(view(2));
  expect(screen.getByRole("searchbox")).toHaveValue("  needle  ");
  expect(screen.getByText(/No matches for/)).toBeInTheDocument();
  expect(authenticatedFetch).toHaveBeenCalledTimes(2);
});

it("submits content and options explicitly, highlights results and opens the exact position", async () => {
  const onFileSelect = vi.fn();
  let resolve!: (response: Response) => void;
  vi.mocked(authenticatedFetch).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const client = new QueryClient();
  const view = (conversationId: string) => (
    <QueryClientProvider client={client}>
      <ContentSearchPanel
        key={conversationId}
        conversationId={conversationId}
        onFileSelect={onFileSelect}
      />
    </QueryClientProvider>
  );
  const { rerender } = render(view("session-one"));
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "  Needle.*  " } });
  fireEvent.click(screen.getByLabelText("Match case"));
  fireEvent.click(screen.getByLabelText("Regular expression"));
  expect(authenticatedFetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
  const url = new URL(String(vi.mocked(authenticatedFetch).mock.calls[0][0]), "http://test");
  expect(url.pathname).toBe(
    "/v1/sessions/session-one/resources/environments/default/content-search",
  );
  expect(Object.fromEntries(url.searchParams)).toEqual({
    q: "  Needle.*  ",
    case_sensitive: "true",
    regex: "true",
  });
  expect(screen.getByText("Searching…")).toBeInTheDocument();
  resolve(
    new Response(
      JSON.stringify({
        matches: [
          {
            path: "src/main.py",
            line: 351,
            column: 4,
            text: "😀 Needle",
            match_start: 3,
            match_end: 9,
          },
        ],
        truncated: true,
        warning: null,
      }),
    ),
  );
  const result = await screen.findByRole("button", { name: "Open src/main.py at line 351" });
  expect(screen.getByText("Needle").tagName).toBe("MARK");
  expect(screen.getByText(/first 1 matching lines/)).toBeInTheDocument();
  fireEvent.click(result);
  expect(onFileSelect).toHaveBeenCalledWith("src/main.py", { line: 351, column: 4 });

  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "unsent draft" } });
  rerender(view("session-two"));
  expect(screen.getByRole("searchbox")).toHaveValue("");
  expect(screen.getByLabelText("Match case")).not.toBeChecked();
  expect(screen.queryByRole("button", { name: /Open src\/main.py/ })).not.toBeInTheDocument();
  rerender(view("session-one"));
  expect(screen.getByRole("searchbox")).toHaveValue("unsent draft");
  expect(screen.getByLabelText("Match case")).toBeChecked();
  expect(screen.getByLabelText("Regular expression")).toBeChecked();
  expect(screen.getByText(/matching lines.*Needle/)).toBeInTheDocument();
  expect(screen.getByText(/first 1 matching lines/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Open src/main.py at line 351" }));
  expect(onFileSelect).toHaveBeenLastCalledWith("src/main.py", { line: 351, column: 4 });
  expect(authenticatedFetch).toHaveBeenCalledTimes(1);
});

it("keeps an in-flight search across switches without mixing conversations", async () => {
  let resolveFirst!: (response: Response) => void;
  vi.mocked(authenticatedFetch)
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
    )
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ matches: [], truncated: false, warning: null })),
    );
  const client = new QueryClient();
  const view = (conversationId: string) => (
    <QueryClientProvider client={client}>
      <ContentSearchPanel
        key={conversationId}
        conversationId={conversationId}
        onFileSelect={vi.fn()}
      />
    </QueryClientProvider>
  );
  const { rerender } = render(view("session-one"));
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "first" } });
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
  rerender(view("session-two"));
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "second" } });
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  expect(await screen.findByText("No matches for “second”.")).toBeInTheDocument();
  rerender(view("session-one"));
  expect(screen.getByText("Searching…")).toBeInTheDocument();
  expect(screen.getByRole("searchbox")).toHaveValue("first");
  rerender(view("session-two"));
  resolveFirst(new Response(JSON.stringify({ matches: [], truncated: false, warning: null })));
  await waitFor(() => expect(client.isFetching()).toBe(0));
  expect(screen.getByText("No matches for “second”.")).toBeInTheDocument();
  rerender(view("session-one"));
  expect(await screen.findByText("No matches for “first”.")).toBeInTheDocument();
  expect(authenticatedFetch).toHaveBeenCalledTimes(2);
});

it("hides a previous error while retrying and displays runner warnings", async () => {
  let resolveRetry!: (response: Response) => void;
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ matches: [], truncated: false, warning: null })),
    )
    .mockResolvedValueOnce(new Response(JSON.stringify({ error: "regex parse error" })))
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveRetry = resolve;
        }),
    );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ContentSearchPanel conversationId="session-one" onFileSelect={vi.fn()} />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "needle" } });
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  expect(await screen.findByText("No matches for “needle”.")).toBeInTheDocument();
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  expect(await screen.findByRole("alert")).toHaveTextContent("regex parse error");
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  expect(await screen.findByText("Searching…")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  const warning = "Some files or ignore rules could not be read.";
  resolveRetry(new Response(JSON.stringify({ matches: [], truncated: true, warning })));
  expect(await screen.findByText("No matches for “needle”.")).toBeInTheDocument();
  expect(screen.getByText(warning)).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it.each([
  [502, "<html>Bad gateway</html>"],
  [200, ""],
  [200, "{}"],
  [200, "null"],
])("handles an invalid search response (%s, %s)", async (status, body) => {
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response(body, { status }));
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ContentSearchPanel conversationId="session-one" onFileSelect={vi.fn()} />
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "needle" } });
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  expect(await screen.findByRole("alert")).toHaveTextContent("Content search is unavailable.");
});
