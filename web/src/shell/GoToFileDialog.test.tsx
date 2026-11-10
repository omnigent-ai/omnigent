import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type ReactNode, useState } from "react";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(),
  useSessionHostOnline: vi.fn(),
}));

import { useSessionHostOnline, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";

import { captureGoToFileOpener, GoToFileDialog } from "./GoToFileDialog";

const runnerOnline = vi.mocked(useSessionRunnerOnline);
const hostOnline = vi.mocked(useSessionHostOnline);
const fetchMock = vi.fn();

interface SearchAnswer {
  paths?: string[];
  directories?: string[];
  hasMore?: boolean;
  truncated?: boolean;
  status?: number;
}

function searchResponse({
  paths = [],
  directories = [],
  hasMore = false,
  truncated = false,
  status = 200,
}: SearchAnswer): Response {
  const entry = (path: string, type: string) => ({
    id: path,
    name: path.split("/").pop(),
    path,
    type,
    bytes: 1,
    modified_at: 0,
  });
  const body = {
    object: "list",
    data: [
      ...directories.map((path) => entry(path, "directory")),
      ...paths.map((path) => entry(path, "file")),
    ],
    has_more: hasMore,
    truncated,
  };
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

function queryOf(input: RequestInfo | URL): string {
  return new URL(String(input), "http://test").searchParams.get("q") ?? "";
}

/** Answers searches by query; a query without an answer stays pending until released. */
function serveSearches(answers: Record<string, SearchAnswer>) {
  const pending = new Map<string, () => void>();
  fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const query = queryOf(input);
    const answer = answers[query];
    if (answer) return Promise.resolve(searchResponse(answer));
    return new Promise<Response>((resolve, reject) => {
      pending.set(query, () => resolve(searchResponse(answers[query] ?? {})));
      init?.signal?.addEventListener("abort", () =>
        reject(new DOMException("aborted", "AbortError")),
      );
    });
  });
  return {
    release(query: string, answer: SearchAnswer) {
      answers[query] = answer;
      pending.get(query)?.();
    },
  };
}

function ignoreOpen() {}

function Harness({
  initialOpen = true,
  conversationId = "conv_1",
  onOpenFile = ignoreOpen,
  opener = null,
  children,
}: {
  initialOpen?: boolean;
  conversationId?: string;
  onOpenFile?: (path: string) => void;
  opener?: HTMLElement | null;
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(initialOpen);
  return (
    <>
      {children}
      <button type="button" onClick={() => setOpen(true)}>
        reopen
      </button>
      <GoToFileDialog
        open={open}
        onOpenChange={setOpen}
        conversationId={conversationId}
        onOpenFile={onOpenFile}
        opener={opener}
      />
    </>
  );
}

function renderDialog(props: Parameters<typeof Harness>[0] = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
  const view = render(
    <QueryClientProvider client={client}>
      <Harness {...props} />
    </QueryClientProvider>,
  );
  return { ...view, client };
}

/** Refetches the shown query, as a window refocus or invalidation would. */
async function refresh(client: QueryClient) {
  await act(async () => {
    await client.invalidateQueries({ queryKey: ["workspace-file-search"] });
  });
}

function searchInput(): HTMLInputElement {
  return screen.getByTestId("go-to-file-input") as HTMLInputElement;
}

function typeQuery(text: string) {
  fireEvent.change(searchInput(), { target: { value: text } });
}

function optionNames(): string[] {
  return screen.queryAllByRole("option").map((option) => option.textContent ?? "");
}

function selectedName(): string | undefined {
  return (
    screen
      .queryAllByRole("option")
      .find((option) => option.getAttribute("aria-selected") === "true")?.textContent ?? undefined
  );
}

function statusText(): string | undefined {
  return screen.queryByRole("status")?.textContent ?? undefined;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  runnerOnline.mockReturnValue(true);
  hostOnline.mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.resetAllMocks();
  document.body.innerHTML = "";
});

describe("GoToFileDialog search and opening", () => {
  it("searches after a pause, ranks the files and opens the chosen one", async () => {
    serveSearches({ app: { paths: ["src/lib/mapper.ts", "src/App.tsx", "app.config.ts"] } });
    const onOpenFile = vi.fn();
    renderDialog({ onOpenFile });
    expect(statusText()).toBe("Type part of a file name or path.");
    expect(fetchMock).not.toHaveBeenCalled();

    typeQuery("app");
    expect(statusText()).toBe("Searching…");
    await waitFor(() => expect(optionNames()).toHaveLength(3));
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "/v1/sessions/conv_1/resources/environments/default/search?limit=500&q=app",
    );
    expect(optionNames()[0]).toBe("app.config.ts");
    expect(selectedName()).toBe("app.config.ts");
    expect(statusText()).toBeUndefined();

    fireEvent.keyDown(searchInput(), { key: "ArrowDown" });
    fireEvent.keyDown(searchInput(), { key: "Enter" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // The file opens after the dialog has closed.
    expect(onOpenFile).toHaveBeenCalledExactlyOnceWith("src/App.tsx");
  });

  it("opens a clicked file and resets the query when reopened", async () => {
    serveSearches({ main: { paths: ["src/main.ts"] } });
    const onOpenFile = vi.fn();
    renderDialog({ onOpenFile });
    typeQuery("main");
    fireEvent.click(await screen.findByRole("option", { name: /main\.ts/ }));
    await waitFor(() => expect(onOpenFile).toHaveBeenCalledWith("src/main.ts"));
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "reopen" }));
    expect(searchInput().value).toBe("");
  });

  it("gives each row an encoded value, so paths differing only in spaces stay distinct", async () => {
    serveSearches({ notes: { paths: ["notes.md", "notes.md "] } });
    renderDialog();
    typeQuery("notes");
    await waitFor(() => expect(screen.queryAllByRole("option")).toHaveLength(2));
    const values = screen
      .queryAllByRole("option")
      .map((option) => option.getAttribute("data-value"));
    expect(new Set(values).size).toBe(2);
    expect(values).toContain("file:notes.md%20");
  });

  it("names and describes the dialog", () => {
    renderDialog();
    const dialog = screen.getByRole("dialog", { name: "Go to File" });
    expect(dialog).toHaveAccessibleDescription(/by part of its name or path/);
  });
});

describe("GoToFileDialog coverage and empty states", () => {
  it.each([
    [
      { paths: ["a/x.ts"], hasMore: true },
      "Showing the first matches. Keep typing to narrow the search.",
    ],
    [
      { paths: ["a/x.ts"], truncated: true },
      "The search stopped before covering the whole workspace. Keep typing to narrow it.",
    ],
    [{ paths: [] }, "No files match “x”."],
    [
      { paths: [], truncated: true },
      "No matches found before the search stopped. Keep typing to narrow it.",
    ],
    [
      { directories: ["x1", "x2"], hasMore: true },
      "No files found among the first matches. Keep typing to narrow the search.",
    ],
  ])("settled answer %o reads %s", async (answer, copy) => {
    serveSearches({ x: answer });
    renderDialog();
    typeQuery("x");
    await waitFor(() => expect(statusText()).toBe(copy));
  });

  it("draws the top 100 of a full 500-entry response and says more matched", async () => {
    const paths = Array.from({ length: 500 }, (_, index) => `dir/file${index}.ts`);
    serveSearches({ file: { paths } });
    renderDialog();
    typeQuery("file");
    await waitFor(() =>
      expect(statusText()).toBe("Showing the top 100 matches. Keep typing to narrow the search."),
    );
    expect(screen.getAllByRole("option")).toHaveLength(100);
  });

  it("says more matched than shown even when the server's list was complete", async () => {
    const paths = Array.from({ length: 150 }, (_, index) => `src/item${index}.ts`);
    serveSearches({ item: { paths } });
    renderDialog();
    typeQuery("item");
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(100));
    expect(statusText()).toBe("Showing the top 100 matches. Keep typing to narrow the search.");
  });

  it("treats a full response as incomplete even when few files remain after folders", async () => {
    const directories = Array.from({ length: 498 }, (_, index) => `d${index}`);
    serveSearches({ d: { paths: ["d/one.ts", "d/two.ts"], directories } });
    renderDialog();
    typeQuery("d");
    await waitFor(() => expect(optionNames()).toHaveLength(2));
    expect(statusText()).toBe("Showing the first matches. Keep typing to narrow the search.");
  });

  it("shows the error even when the very first request fails", async () => {
    serveSearches({ boom: { status: 500 } });
    renderDialog();
    typeQuery("boom");
    await waitFor(() =>
      expect(statusText()).toBe(
        "Couldn't search this workspace. Check that the session is connected and try again.",
      ),
    );
  });

  it("says the files are unavailable instead of searching forever", async () => {
    runnerOnline.mockReturnValue(false);
    hostOnline.mockReturnValue(false);
    renderDialog();
    typeQuery("main");
    await waitFor(() =>
      expect(statusText()).toBe("This session's files aren't available right now."),
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("says the files are unavailable when the workspace can't be searched while online", async () => {
    runnerOnline.mockReturnValue(true);
    serveSearches({ main: { status: 404 } });
    renderDialog();
    typeQuery("main");
    await waitFor(() =>
      expect(statusText()).toBe("This session's files aren't available right now."),
    );
    expect(screen.queryByText(/No files match/)).toBeNull();
  });

  it("never reads as finished while the current query is still in flight", async () => {
    const searches = serveSearches({ ab: { paths: [] } });
    renderDialog();
    typeQuery("ab");
    await waitFor(() => expect(statusText()).toBe("No files match “ab”."));

    typeQuery("abc");
    expect(statusText()).toBe("Searching…");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(statusText()).toBe("Searching…");
    act(() => searches.release("abc", { paths: ["abc.ts"], hasMore: true }));
    await waitFor(() => expect(optionNames()).toEqual(["abc.ts"]));
    expect(statusText()).toBe("Showing the first matches. Keep typing to narrow the search.");
  });
});

describe("GoToFileDialog stale results", () => {
  it("shows an earlier query's rows only while they still match, and Enter skips the rest", async () => {
    const searches = serveSearches({ app: { paths: ["src/App.tsx", "src/apple.ts"] } });
    const onOpenFile = vi.fn();
    renderDialog({ onOpenFile });
    typeQuery("app");
    await waitFor(() => expect(optionNames()).toHaveLength(2));

    typeQuery("appl");
    expect(optionNames()).toEqual(["apple.tssrc"]);
    typeQuery("applz");
    expect(optionNames()).toEqual([]);
    fireEvent.keyDown(searchInput(), { key: "Enter" });
    expect(onOpenFile).not.toHaveBeenCalled();
    expect(statusText()).toBe("Searching…");
    act(() => searches.release("applz", { paths: [] }));
  });

  it("drops results when the session changes", async () => {
    serveSearches({ app: { paths: ["src/App.tsx"] } });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(
      <QueryClientProvider client={client}>
        <Harness key="conv_1" conversationId="conv_1" />
      </QueryClientProvider>,
    );
    typeQuery("app");
    await waitFor(() => expect(optionNames()).toHaveLength(1));
    view.rerender(
      <QueryClientProvider client={client}>
        <Harness key="conv_2" conversationId="conv_2" />
      </QueryClientProvider>,
    );
    expect(searchInput().value).toBe("");
    expect(optionNames()).toEqual([]);
  });
});

describe("GoToFileDialog selection", () => {
  it("keeps the user's row through a refresh that reorders, and resets on a new query", async () => {
    const answers: Record<string, SearchAnswer> = {
      lib: { paths: ["lib/b.ts", "lib/c.ts", "lib/a.ts"] },
      "lib/c": { paths: ["lib/c.ts"] },
    };
    serveSearches(answers);
    const { client } = renderDialog();
    typeQuery("lib");
    await waitFor(() => expect(optionNames()).toHaveLength(3));
    expect(selectedName()).toBe("a.tslib");
    fireEvent.keyDown(searchInput(), { key: "ArrowDown" });
    expect(selectedName()).toBe("b.tslib");

    // A refetch for the same query brings a new first row.
    answers.lib = { paths: ["lib/0.ts", "lib/b.ts", "lib/c.ts", "lib/a.ts"] };
    await refresh(client);
    await waitFor(() => expect(optionNames()).toHaveLength(4));
    expect(optionNames()[0]).toBe("0.tslib");
    expect(selectedName()).toBe("b.tslib");

    typeQuery("lib/c");
    await waitFor(() => expect(optionNames()).toEqual(["c.tslib"]));
    expect(selectedName()).toBe("c.tslib");
  });

  it("moves to the first row when the selected one disappears", async () => {
    const answers: Record<string, SearchAnswer> = { src: { paths: ["src/a.ts", "src/b.ts"] } };
    serveSearches(answers);
    const { client } = renderDialog();
    typeQuery("src");
    await waitFor(() => expect(optionNames()).toHaveLength(2));
    fireEvent.keyDown(searchInput(), { key: "ArrowDown" });
    expect(selectedName()).toBe("b.tssrc");
    answers.src = { paths: ["src/a.ts", "src/c.ts"] };
    await refresh(client);
    await waitFor(() => expect(optionNames()).toEqual(["a.tssrc", "c.tssrc"]));
    expect(selectedName()).toBe("a.tssrc");
  });
});

describe("GoToFileDialog focus", () => {
  it("returns focus to the element focused before it opened when Esc closes it", async () => {
    const client = new QueryClient();
    function Opener() {
      const [open, setOpen] = useState(false);
      const [opener, setOpener] = useState<HTMLElement | null>(null);
      return (
        <>
          <button
            type="button"
            onKeyDown={(event) => {
              if (event.key === "p") {
                setOpener(captureGoToFileOpener());
                setOpen(true);
              }
            }}
          >
            composer
          </button>
          <GoToFileDialog
            open={open}
            onOpenChange={setOpen}
            conversationId="conv_1"
            onOpenFile={() => {}}
            opener={opener}
          />
        </>
      );
    }
    render(
      <QueryClientProvider client={client}>
        <Opener />
      </QueryClientProvider>,
    );
    const composer = screen.getByRole("button", { name: "composer" });
    composer.focus();
    fireEvent.keyDown(composer, { key: "p" });
    await waitFor(() => expect(searchInput()).toHaveFocus());
    fireEvent.keyDown(searchInput(), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(composer).toHaveFocus();
  });

  it("doesn't restore focus to an opener that was removed", async () => {
    const gone = document.createElement("button");
    document.body.append(gone);
    renderDialog({ opener: gone });
    gone.remove();
    fireEvent.keyDown(searchInput(), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement?.isConnected).toBe(true);
    expect(document.activeElement).not.toBe(gone);
  });

  it("leaves focus where opening the file put it, such as an unsaved-changes prompt", async () => {
    serveSearches({ main: { paths: ["src/main.ts"] } });
    const opener = document.createElement("button");
    document.body.append(opener);
    const prompt = document.createElement("button");
    prompt.textContent = "Discard changes";
    renderDialog({
      opener,
      onOpenFile: () => {
        document.body.append(prompt);
        prompt.focus();
      },
    });
    typeQuery("main");
    await screen.findByRole("option", { name: /main\.ts/ });
    fireEvent.keyDown(searchInput(), { key: "Enter" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(prompt).toHaveFocus();
  });

  it("doesn't capture an opener inside another dialog, such as the ⌘K palette", () => {
    const palette = document.createElement("div");
    palette.setAttribute("role", "dialog");
    const paletteInput = document.createElement("input");
    palette.append(paletteInput);
    document.body.append(palette);
    paletteInput.focus();
    expect(captureGoToFileOpener()).toBeNull();
    palette.remove();

    const plain = document.createElement("button");
    document.body.append(plain);
    plain.focus();
    expect(captureGoToFileOpener()).toBe(plain);
  });
});
