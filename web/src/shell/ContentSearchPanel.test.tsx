import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { requestTextSearchFocus } from "@/store/fileFinder";
import { EMPTY_CONTENT_SEARCH } from "@/lib/contentSearch";
import { ContentSearchPanel } from "./ContentSearchPanel";
import { contentSearchInputKey } from "./contentSearchCache";

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
  const filters = screen.getByRole("button", { name: "Filter files" });
  expect(screen.getByRole("searchbox").nextElementSibling).toBe(filters);
  expect(filters).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByLabelText("Include")).not.toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Filter files" }));
  expect(filters).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByLabelText("Include")).toBeVisible();
  fireEvent.change(screen.getByLabelText("Include"), { target: { value: "src/**" } });
  fireEvent.change(screen.getByLabelText("Exclude"), { target: { value: "*.test.ts" } });
  fireEvent.click(screen.getByRole("button", { name: "Filter files" }));
  expect(filters).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByLabelText("Include")).not.toBeVisible();
  expect(authenticatedFetch).not.toHaveBeenCalled();
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  expect(await screen.findByText(/No matches for/)).toBeInTheDocument();
  expect(screen.getByRole("searchbox")).toHaveValue("  needle  ");
  expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Filter files" }));
  fireEvent.change(screen.getByLabelText("Include"), { target: { value: "" } });
  fireEvent.change(screen.getByLabelText("Exclude"), { target: { value: "" } });
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(2));
  const cleared = new URL(String(vi.mocked(authenticatedFetch).mock.calls[1][0]), "http://test");
  expect(cleared.searchParams.get("include")).toBe("");
  expect(cleared.searchParams.get("exclude")).toBe("");
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
  fireEvent.click(screen.getByRole("button", { name: "Filter files" }));
  fireEvent.change(screen.getByLabelText("Include"), { target: { value: "src/**, *.{py,txt}" } });
  fireEvent.change(screen.getByLabelText("Exclude"), {
    target: { value: "**/tests/**, a & b/**" },
  });
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
    include: "src/**, *.{py,txt}",
    exclude: "**/tests/**, a & b/**",
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
  expect(screen.getByLabelText("Include")).toHaveValue("");
  expect(screen.getByLabelText("Exclude")).toHaveValue("");
  expect(screen.queryByRole("button", { name: /Open src\/main.py/ })).not.toBeInTheDocument();
  rerender(view("session-one"));
  expect(screen.getByRole("searchbox")).toHaveValue("unsent draft");
  expect(screen.getByLabelText("Match case")).toBeChecked();
  expect(screen.getByLabelText("Regular expression")).toBeChecked();
  expect(screen.getByLabelText("Include")).toHaveValue("src/**, *.{py,txt}");
  expect(screen.getByLabelText("Exclude")).toHaveValue("**/tests/**, a & b/**");
  expect(screen.getByRole("button", { name: "Filter files" })).toBeInTheDocument();
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

it("searches whole words through the existing regex route and keeps the option", async () => {
  vi.mocked(authenticatedFetch).mockImplementation(
    async () => new Response(JSON.stringify({ matches: [], truncated: false, warning: null })),
  );
  const user = userEvent.setup();
  const client = new QueryClient();
  const view = (request: number) => (
    <QueryClientProvider client={client}>
      <ContentSearchPanel key={request} conversationId="session-one" onFileSelect={vi.fn()} />
    </QueryClientProvider>
  );
  const { rerender } = render(view(1));
  const wholeWord = screen.getByRole("checkbox", { name: "Whole word" });
  expect(wholeWord).not.toBeChecked();
  wholeWord.focus();
  await user.keyboard(" ");
  expect(wholeWord).toBeChecked();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "C++" } });
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  expect(await screen.findByText("No matches for “C++”.")).toBeInTheDocument();
  const url = new URL(String(vi.mocked(authenticatedFetch).mock.calls[0][0]), "http://test");
  expect(url.searchParams.get("q")).toBe("\\bC\\+\\+");
  expect(url.searchParams.get("regex")).toBe("true");
  rerender(view(2));
  expect(screen.getByRole("checkbox", { name: "Whole word" })).toBeChecked();
});

it("refuses a whole-word search that outgrows the server's limit without sending it", async () => {
  vi.mocked(authenticatedFetch).mockImplementation(
    async () => new Response(JSON.stringify({ matches: [], truncated: false, warning: null })),
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ContentSearchPanel conversationId="session-one" onFileSelect={vi.fn()} />
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "a".repeat(4096) } });
  fireEvent.click(screen.getByLabelText("Whole word"));
  expect(screen.getByRole("alert")).toHaveTextContent(
    "This search is too long to match whole words. Shorten it, or turn off Whole word.",
  );
  expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  expect(authenticatedFetch).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "a".repeat(4092) } });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "a".repeat(4096) } });
  fireEvent.click(screen.getByLabelText("Whole word"));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
  const url = new URL(String(vi.mocked(authenticatedFetch).mock.calls[0][0]), "http://test");
  expect(url.searchParams.get("q")).toBe("a".repeat(4096));
  expect(url.searchParams.get("regex")).toBe("false");
});

it("refuses whole-word expressions that could escape the boundaries", async () => {
  vi.mocked(authenticatedFetch).mockImplementation(
    async () => new Response(JSON.stringify({ matches: [], truncated: false, warning: null })),
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ContentSearchPanel conversationId="session-one" onFileSelect={vi.fn()} />
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "foo)|(bar" } });
  fireEvent.click(screen.getByLabelText("Regular expression"));
  fireEvent.click(screen.getByLabelText("Whole word"));
  expect(screen.getByRole("alert")).toHaveTextContent(
    "This expression closes a group it never opened, so it can’t match whole words.",
  );
  expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  expect(authenticatedFetch).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "(?x)[ ](]foo)|(bar" } });
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Whole word can’t be used with verbose mode (?x).",
  );
  expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  expect(authenticatedFetch).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "foo)|(bar" } });
  // With Whole word off the expression goes to ripgrep unchanged, as before.
  fireEvent.click(screen.getByLabelText("Whole word"));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  fireEvent.submit(screen.getByRole("searchbox").closest("form")!);
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
  const url = new URL(String(vi.mocked(authenticatedFetch).mock.calls[0][0]), "http://test");
  expect(url.searchParams.get("q")).toBe("foo)|(bar");
  expect(url.searchParams.get("regex")).toBe("true");
});

it("shows a search handed over from Go to File, whether or not the page already shows", async () => {
  const { handSearchToSearchPage } = await import("./contentSearchCache");
  const client = new QueryClient();
  const handed = {
    query: "needle",
    caseSensitive: true,
    regex: false,
    wholeWord: false,
    include: "src/**",
    exclude: "",
  };
  const found = {
    matches: [
      { path: "src/a.ts", line: 3, column: 1, text: "needle", match_start: 0, match_end: 6 },
    ],
    truncated: false,
    warning: null,
  };
  // Handed over before the page shows: it opens on that search and its lines.
  await handSearchToSearchPage(client, "session-one", handed, found);
  render(
    <QueryClientProvider client={client}>
      <ContentSearchPanel conversationId="session-one" onFileSelect={vi.fn()} />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("searchbox")).toHaveValue("needle");
  expect(screen.getByRole("checkbox", { name: "Match case" })).toBeChecked();
  expect(screen.getByLabelText("Include")).toBeVisible();
  expect(screen.getByLabelText("Include")).toHaveValue("src/**");
  expect(screen.getByRole("button", { name: "Open src/a.ts at line 3" })).toBeInTheDocument();
  expect(authenticatedFetch).not.toHaveBeenCalled();

  // Handed over while it shows: it takes the new search in place of what was typed.
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "typed here" } });
  await act(() =>
    handSearchToSearchPage(
      client,
      "session-one",
      { ...handed, query: "other", include: "" },
      {
        ...found,
        matches: [{ ...found.matches[0]!, path: "src/b.ts", text: "other" }],
      },
    ),
  );
  await waitFor(() => expect(screen.getByRole("searchbox")).toHaveValue("other"));
  expect(screen.getByRole("button", { name: "Open src/b.ts at line 3" })).toBeInTheDocument();
});

it("takes the keyboard when a text search asks this session's page for it", () => {
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <ContentSearchPanel conversationId="session-one" onFileSelect={vi.fn()} />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("searchbox")).not.toHaveFocus();
  // Another session's request leaves the field alone.
  act(() => requestTextSearchFocus("session-two"));
  expect(screen.getByRole("searchbox")).not.toHaveFocus();
  act(() => requestTextSearchFocus("session-one"));
  expect(screen.getByRole("searchbox")).toHaveFocus();
});

it("keeps remembered filters folded when nothing was handed over", () => {
  const client = new QueryClient();
  client.setQueryData(contentSearchInputKey("session-one"), {
    ...EMPTY_CONTENT_SEARCH,
    query: "needle",
    include: "src/**",
  });
  render(
    <QueryClientProvider client={client}>
      <ContentSearchPanel conversationId="session-one" onFileSelect={vi.fn()} />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("searchbox")).toHaveValue("needle");
  expect(screen.getByLabelText("Include")).not.toBeVisible();
  expect(authenticatedFetch).not.toHaveBeenCalled();
});

it("keeps a handed search over the page's own search that answers later", async () => {
  const { handSearchToSearchPage } = await import("./contentSearchCache");
  let answerOwnSearch: (response: Response) => void = () => {};
  vi.mocked(authenticatedFetch).mockImplementation(
    () =>
      new Promise<Response>((resolve) => {
        answerOwnSearch = resolve;
      }),
  );
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <ContentSearchPanel conversationId="session-one" onFileSelect={vi.fn()} />
    </QueryClientProvider>,
  );
  const field = screen.getByRole("searchbox");
  fireEvent.change(field, { target: { value: "old" } });
  fireEvent.submit(field.closest("form")!);
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalled());

  // Go to File hands its search over while the page's own one still runs.
  const handedMatch = { path: "src/new.ts", line: 2, column: 1, text: "new", match_start: 0 };
  await act(() =>
    handSearchToSearchPage(
      client,
      "session-one",
      { ...EMPTY_CONTENT_SEARCH, query: "new" },
      { matches: [{ ...handedMatch, match_end: 3 }], truncated: false, warning: null },
    ),
  );
  await waitFor(() => expect(field).toHaveValue("new"));
  await act(async () => {
    answerOwnSearch(
      new Response(
        JSON.stringify({
          matches: [{ ...handedMatch, path: "src/old.ts", text: "old", match_end: 3 }],
          truncated: false,
          warning: null,
        }),
      ),
    );
  });
  // Let the late answer settle wherever it would land.
  await act(
    () =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, 20);
      }),
  );
  expect(field).toHaveValue("new");
  expect(screen.getByRole("button", { name: "Open src/new.ts at line 2" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Open src/old.ts at line 2" })).toBeNull();
});

it("runs a search handed over without its answer, in place of the page's older one", async () => {
  const { handSearchToSearchPage } = await import("./contentSearchCache");
  const client = new QueryClient();
  const older = { path: "src/old.ts", line: 5, column: 1, text: "old", match_start: 0 };
  await handSearchToSearchPage(
    client,
    "session-one",
    { ...EMPTY_CONTENT_SEARCH, query: "old" },
    { matches: [{ ...older, match_end: 3 }], truncated: false, warning: null },
  );
  let answer = (_response: Response) => {};
  vi.mocked(authenticatedFetch).mockReturnValue(
    new Promise<Response>((resolve) => {
      answer = resolve;
    }),
  );
  render(
    <QueryClientProvider client={client}>
      <ContentSearchPanel conversationId="session-one" onFileSelect={vi.fn()} />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("button", { name: "Open src/old.ts at line 5" })).toBeInTheDocument();
  // Go to File's own search hadn't answered yet, so only the query comes over, and runs here.
  await act(() =>
    handSearchToSearchPage(
      client,
      "session-one",
      { ...EMPTY_CONTENT_SEARCH, query: "new" },
      undefined,
    ),
  );
  await waitFor(() => expect(screen.getByRole("searchbox")).toHaveValue("new"));
  expect(screen.queryByRole("button", { name: "Open src/old.ts at line 5" })).toBeNull();
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
  const url = new URL(String(vi.mocked(authenticatedFetch).mock.calls[0][0]), "http://test");
  expect(url.searchParams.get("q")).toBe("new");
  const found = { path: "src/new.ts", line: 2, column: 1, text: "new", match_start: 0 };
  act(() =>
    answer(
      new Response(
        JSON.stringify({ matches: [{ ...found, match_end: 3 }], truncated: false, warning: null }),
      ),
    ),
  );
  expect(
    await screen.findByRole("button", { name: "Open src/new.ts at line 2" }),
  ).toBeInTheDocument();
});

it("runs a search handed over without its answer once the page opens", async () => {
  const { handSearchToSearchPage } = await import("./contentSearchCache");
  const client = new QueryClient();
  await handSearchToSearchPage(
    client,
    "session-one",
    { ...EMPTY_CONTENT_SEARCH, query: "later" },
    undefined,
  );
  render(
    <QueryClientProvider client={client}>
      <ContentSearchPanel conversationId="session-one" onFileSelect={vi.fn()} />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("searchbox")).toHaveValue("later");
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
  const url = new URL(String(vi.mocked(authenticatedFetch).mock.calls[0][0]), "http://test");
  expect(url.searchParams.get("q")).toBe("later");
});
