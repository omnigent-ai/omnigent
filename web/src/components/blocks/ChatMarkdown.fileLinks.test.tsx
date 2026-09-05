// End-to-end rendering of a markdown link whose href is a workspace file.
//
// Agents link files they just wrote (`[foo.md](/abs/ws/foo.md)`). Two ways this
// used to go wrong, both reproduced here so a regression is loud:
//
//   - An absolute path stayed a real anchor, so clicking it navigated the app
//     origin and the server answered {"detail":"Not Found"}.
//   - A path with no leading slash had its href stripped and " [blocked]"
//     appended, which read as though the app had censored the link.
//
// Both should instead open the FileViewer, exactly as an inline-code path does.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
import { showToast } from "@/components/ui/toast";

const toastMock = vi.mocked(showToast);
const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  toastMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(cleanup);

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

// Absolute (base=host) listings echo entries as names relative to the listed
// dir, with the dir itself in `base` — mirror that wire shape so what's
// tested includes the existence check re-attaching the dir.
function dirListing(paths: string[]): Response {
  const parent = paths.length ? paths[0].slice(0, paths[0].lastIndexOf("/")) || "/" : "/";
  return jsonResponse({
    object: "list",
    base: parent,
    data: paths.map((path) => ({
      id: path,
      name: path.split("/").pop(),
      path: path.split("/").pop(),
      type: "file",
      bytes: 5,
      modified_at: 1,
    })),
    has_more: false,
  });
}

const WORKSPACE = "/home/u/ws";

// Module scope so the provider value is a constant (jsx-no-constructed-context-values);
// `renderMarkdown` swaps the changed-file list and resets the spy per test.
const openFile = vi.fn();
let changedPaths: string[] = [];
const FILE_VIEWER = {
  openFile,
  openGithubTab: () => {},
  isChangedPath: (p: string) => changedPaths.includes(p),
  conversationId: undefined as string | undefined,
  workspaceRoot: WORKSPACE,
  workspaceHome: "/home/u",
};

// Same shape, but with a live conversation id so the existence check can run
// (its query is disabled without one — exactly what the base fixture relies
// on to keep those tests network-free).
const FILE_VIEWER_WITH_SESSION = {
  ...FILE_VIEWER,
  conversationId: "conv_1",
};

function renderMarkdown(
  markdown: string,
  changed: string[] = [],
  viewer: typeof FILE_VIEWER = FILE_VIEWER,
): void {
  changedPaths = changed;
  openFile.mockClear();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <FileViewerContext.Provider value={viewer}>
        <FilePathAwareMessageResponse>{markdown}</FilePathAwareMessageResponse>
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
}

describe("markdown links to workspace files", () => {
  it("opens the FileViewer for an absolute path instead of navigating", () => {
    renderMarkdown(`[proposal.md](${WORKSPACE}/docs/proposal.md)`, ["docs/proposal.md"]);

    const link = screen.getByRole("button", { name: "proposal.md" });
    // No href at all: the parked fragment must not survive as clickable.
    expect(link).not.toHaveAttribute("href");

    fireEvent.click(link);
    expect(openFile).toHaveBeenCalledWith("docs/proposal.md");
  });

  it("opens the FileViewer for a relative path instead of showing it as blocked", () => {
    renderMarkdown("[notes.md](docs/notes.md)", ["docs/notes.md"]);

    expect(screen.queryByText(/\[blocked\]/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "notes.md" }));
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });

  it("opens on keyboard activation", () => {
    renderMarkdown("[notes.md](docs/notes.md)", ["docs/notes.md"]);

    fireEvent.keyDown(screen.getByRole("button", { name: "notes.md" }), { key: "Enter" });
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });

  it("leaves an external link as a real anchor", () => {
    renderMarkdown("[docs](https://example.com/page)");

    const link = screen.getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("href", "https://example.com/page");
  });

  // Overriding the `a` slot replaces Streamdown's link component, so its
  // styling and marker attribute have to be reproduced. index.css keys the
  // pointer cursor and the table-cell overflow-wrap rule (which stops a
  // link-only table column collapsing to ~2ch) on the attribute.
  it.each([
    ["an external link", "[docs](https://example.com/page)", "docs", [] as string[]],
    ["a workspace file link", "[notes.md](docs/notes.md)", "notes.md", ["docs/notes.md"]],
  ])("keeps Streamdown's link styling on %s", (_label, markdown, name, changed) => {
    renderMarkdown(markdown, changed);

    const link = screen.getByText(name).closest("a");
    expect(link).toHaveAttribute("data-streamdown", "link");
    expect(link).toHaveClass("wrap-anywhere", "font-medium", "text-primary", "underline");
  });

  it("keeps the source hast node out of the DOM", () => {
    // Streamdown passes `node` to every override; spreading it onto the element
    // renders a literal node="[object Object]" attribute.
    renderMarkdown("[docs](https://example.com/page) and `docs/notes.md`", ["docs/notes.md"]);

    expect(screen.getByRole("link", { name: "docs" })).not.toHaveAttribute("node");
    expect(screen.getByRole("button", { name: "docs/notes.md" })).not.toHaveAttribute("node");
  });

  it("renders a path that names no workspace file as plain text, not a dead link", () => {
    // Not in the changed list and no conversation id, so no existence check can
    // confirm it, so it must not become an anchor on the parked fragment.
    renderMarkdown("[ghost.md](docs/ghost.md)");

    expect(screen.queryByRole("link", { name: "ghost.md" })).toBeNull();
    expect(screen.queryByRole("button", { name: "ghost.md" })).toBeNull();
    expect(screen.getByText("ghost.md")).toBeInTheDocument();
    expect(screen.queryByText(/\[blocked\]/)).toBeNull();
  });

  it("still linkifies an inline-code path", () => {
    renderMarkdown("see `docs/notes.md` for detail", ["docs/notes.md"]);

    fireEvent.click(screen.getByRole("button", { name: "docs/notes.md" }));
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });
});

// `path:line` is how agents habitually cite a file. The position is not part of
// the filename, so resolving the span verbatim matched nothing on disk and the
// citation rendered as inert inline code.
describe("cited positions", () => {
  it("opens an inline-code path that carries a line number", () => {
    renderMarkdown("`docs/notes.md:12` has the detail", ["docs/notes.md"]);

    // The span still shows what the agent wrote; only the target drops :12.
    fireEvent.click(screen.getByRole("button", { name: "docs/notes.md:12" }));
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });

  it("opens an absolute inline-code path with a line number", () => {
    // The shape from the report: an absolute path under the workspace root.
    renderMarkdown(`\`${WORKSPACE}/docs/notes.md:1\` contains hi`, ["docs/notes.md"]);

    fireEvent.click(screen.getByRole("button", { name: `${WORKSPACE}/docs/notes.md:1` }));
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });

  it("opens a path citing both line and column", () => {
    renderMarkdown("`docs/notes.md:12:7` is the spot", ["docs/notes.md"]);

    fireEvent.click(screen.getByRole("button", { name: "docs/notes.md:12:7" }));
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });

  it("opens a markdown link whose href carries a line number", () => {
    renderMarkdown("[notes.md](docs/notes.md:12)", ["docs/notes.md"]);

    fireEvent.click(screen.getByRole("button", { name: "notes.md" }));
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });

  it("leaves a trailing-colon span that is not a position alone", () => {
    // "Note:" style prose must not be mistaken for a path with a position.
    renderMarkdown("`docs/notes.md:abc` is not a position", ["docs/notes.md"]);

    expect(screen.queryByRole("button", { name: "docs/notes.md:abc" })).toBeNull();
  });
});

// Files OUTSIDE the workspace root. The FileViewer and filesystem API open
// host-absolute paths (the files panel's browse-anywhere plumbing), so an
// agent-cited outside file must linkify once its existence is confirmed —
// before this, such links dropped to dead text that did nothing when
// clicked, and gave a touch user (no hover title) no feedback at all.
describe("links to files outside the workspace", () => {
  it("linkifies an outside-workspace markdown link and opens it host-absolute", async () => {
    fetchMock.mockResolvedValue(dirListing(["/etc/hosts"]));
    renderMarkdown("[/etc/hosts](/etc/hosts)", [], FILE_VIEWER_WITH_SESSION);

    const link = await screen.findByRole("button", { name: "/etc/hosts" });
    // The existence check listed the ABSOLUTE parent via base=host — never a
    // leading %2F that a slash-merging proxy would collapse.
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("/filesystem/etc?");
    expect(url).toContain("base=host");

    fireEvent.click(link);
    expect(openFile).toHaveBeenCalledWith("/etc/hosts");
  });

  it("linkifies an outside-workspace inline-code path", async () => {
    fetchMock.mockResolvedValue(dirListing(["/etc/hosts"]));
    renderMarkdown("see `/etc/hosts` for the mapping", [], FILE_VIEWER_WITH_SESSION);

    fireEvent.click(await screen.findByRole("button", { name: "/etc/hosts" }));
    expect(openFile).toHaveBeenCalledWith("/etc/hosts");
  });

  it("resolves an outside-workspace home-relative path through the runner home", async () => {
    fetchMock.mockResolvedValue(dirListing(["/home/u/other/notes.md"]));
    renderMarkdown("[notes](~/other/notes.md)", [], FILE_VIEWER_WITH_SESSION);

    fireEvent.click(await screen.findByRole("button", { name: "notes" }));
    expect(openFile).toHaveBeenCalledWith("/home/u/other/notes.md");
  });
});

// A marked link that names no openable file. Silent inertness is only
// acceptable while the answer isn't known yet; once it is, the reference must
// explain itself when activated — on touch there is no hover title, so an
// inert span reads as "tapping does nothing".
describe("dead file links give feedback instead of a silent no-op", () => {
  it("explains a confirmed-missing link on click instead of doing nothing", async () => {
    // Parent listing exists but the file isn't in it → definitively absent.
    fetchMock.mockResolvedValue(dirListing(["/no/such/other.txt"]));
    renderMarkdown("[missing](/no/such/file.txt)", [], FILE_VIEWER_WITH_SESSION);

    const dead = await screen.findByRole("button", { name: "missing" });
    fireEvent.click(dead);
    expect(openFile).not.toHaveBeenCalled();
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect(String(toastMock.mock.calls[0][0])).toContain("/no/such/file.txt");
  });

  it("explains on keyboard activation too", async () => {
    fetchMock.mockResolvedValue(dirListing([]));
    renderMarkdown("[missing](/no/such/file.txt)", [], FILE_VIEWER_WITH_SESSION);

    fireEvent.keyDown(await screen.findByRole("button", { name: "missing" }), { key: "Enter" });
    expect(openFile).not.toHaveBeenCalled();
    expect(toastMock).toHaveBeenCalledTimes(1);
  });

  it("stays inert (no feedback affordance) while the check is in flight", () => {
    // Never resolves → the answer isn't known; claiming "can't open" now
    // would be wrong for a file that's about to be confirmed.
    fetchMock.mockReturnValue(new Promise<Response>(() => {}));
    renderMarkdown("[pending](/no/such/file.txt)", [], FILE_VIEWER_WITH_SESSION);

    expect(screen.queryByRole("button", { name: "pending" })).toBeNull();
    expect(screen.getByText("pending")).toBeInTheDocument();
    expect(toastMock).not.toHaveBeenCalled();
  });
});
