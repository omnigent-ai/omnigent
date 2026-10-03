import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { copyTextMock } = vi.hoisted(() => ({ copyTextMock: vi.fn(() => Promise.resolve()) }));
vi.mock("@/lib/clipboard", () => ({ copyText: copyTextMock }));
import { TooltipProvider } from "@/components/ui/tooltip";
import { RunnerOfflineError, type WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { ROW_STATUS_SLOT_CLASS } from "./fileStatusUtils";
import { FlatFileList } from "./FlatFileList";

afterEach(cleanup);
beforeEach(() => copyTextMock.mockClear());

/** Render FlatFileList with sensible defaults, overriding only what a test needs. */
function renderList(props: Partial<Parameters<typeof FlatFileList>[0]> = {}) {
  return render(
    <TooltipProvider>
      <FlatFileList
        files={undefined}
        isLoading={false}
        isError={false}
        error={null}
        onFileSelect={vi.fn()}
        showHidden={false}
        onShowHidden={vi.fn()}
        searchQuery=""
        sort="alpha"
        conversationId="conv_abc"
        {...props}
      />
    </TooltipProvider>,
  );
}

describe("FlatFileList runner-offline state", () => {
  it("shows the reconnect hint when the runner went offline (session failed)", () => {
    // RunnerOfflineError = the changes fetch's 503. With runnerWentOffline
    // (session status "failed", e.g. host restarted) the panel shows the
    // reconnect hint, NOT the generic "Failed to load" branch.
    renderList({ isError: true, error: new RunnerOfflineError(), runnerWentOffline: true });

    expect(screen.getByText(/agent is asleep/i)).toBeInTheDocument();
    expect(screen.getByText(/send a message in the chat to reconnect/i)).toBeInTheDocument();
    // The raw error text must NOT appear for this recoverable state.
    expect(screen.queryByText(/failed to load/i)).not.toBeInTheDocument();
  });

  it("shows the empty state (not the asleep hint) for a new session that hasn't started", () => {
    // A brand-new session also 503s while its runner connects, but it never
    // went "failed" — runnerWentOffline is false, so it must read as the
    // normal empty state, not alarm the user that the agent is asleep.
    renderList({ isError: true, error: new RunnerOfflineError(), runnerWentOffline: false });

    expect(screen.getByText(/no workspace changes yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/agent is asleep/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/failed to load/i)).not.toBeInTheDocument();
  });

  it("still shows the raw error for a non-runner-offline failure", () => {
    // Generic errors keep the diagnostic "Failed to load: …" text so real
    // failures aren't masked by the reconnect hint.
    renderList({ isError: true, error: new Error("500 Internal Server Error") });

    expect(screen.getByText(/failed to load: 500 internal server error/i)).toBeInTheDocument();
    expect(screen.queryByText(/agent is asleep/i)).not.toBeInTheDocument();
  });
});

describe("FlatFileList status / download alignment", () => {
  it("shows file size using the same caption treatment as the Files tree", () => {
    renderList({
      files: [
        {
          path: "src/app.ts",
          name: "app.ts",
          status: "modified",
          bytes: 2048,
          modified_at: null,
          lines_added: null,
          lines_removed: null,
        },
      ],
    });

    expect(screen.getByText("2.0 KB")).toHaveClass("text-sm", "text-muted-foreground");
  });

  it("keeps status separate while actions replace the far-right file size", () => {
    renderList({
      files: [
        {
          path: "src/app.ts",
          name: "app.ts",
          status: "modified",
          bytes: 2048,
          modified_at: null,
          lines_added: null,
          lines_removed: null,
        },
      ],
    });

    const letter = screen.getByText("M");
    expect(letter.parentElement).toHaveClass(ROW_STATUS_SLOT_CLASS);

    const size = screen.getByText("2.0 KB");
    const slot = size.parentElement;
    expect(slot).toHaveClass("relative");
    expect(size).toHaveClass("group-hover:invisible", "group-has-[:focus-visible]:invisible");
    const download = screen.getByRole("button", { name: /download app\.ts/i });
    const overlay = download.closest("span.absolute") as HTMLElement | null;
    expect(overlay).not.toBeNull();
    expect(slot).toContainElement(overlay);
  });
});

describe("FlatFileList line-change counter", () => {
  it("renders +added and −removed when both counts are present", () => {
    renderList({
      files: [
        {
          path: "src/app.ts",
          name: "app.ts",
          status: "modified",
          bytes: 2048,
          modified_at: null,
          lines_added: 12,
          lines_removed: 3,
        },
      ],
    });

    expect(screen.getByText("+12")).toBeInTheDocument();
    expect(screen.getByText("−3")).toBeInTheDocument();
  });

  it("shows only −removed for a deleted file (added is 0)", () => {
    renderList({
      files: [
        {
          path: "gone.py",
          name: "gone.py",
          status: "deleted",
          bytes: null,
          modified_at: null,
          lines_added: 0,
          lines_removed: 7,
        },
      ],
    });

    expect(screen.getByText("−7")).toBeInTheDocument();
    // +0 still renders (0 is a real, non-null count) but the removed side is
    // the meaningful one for a deletion.
    expect(screen.getByText("+0")).toBeInTheDocument();
  });

  it("omits the counter entirely when both counts are null (binary/untracked/unavailable)", () => {
    renderList({
      files: [
        {
          path: "img.bin",
          name: "img.bin",
          status: "modified",
          bytes: 1024,
          modified_at: null,
          lines_added: null,
          lines_removed: null,
        },
      ],
    });

    expect(screen.queryByText(/^\+/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^−/)).not.toBeInTheDocument();
  });

  it("omits the counter for a mode-only change (both counts 0)", () => {
    // A chmod-only edit shows in numstat as 0/0; a "+0 −0" badge is noise, so
    // suppress it while still rendering a real deletion's −N.
    renderList({
      files: [
        {
          path: "script.sh",
          name: "script.sh",
          status: "modified",
          bytes: 512,
          modified_at: null,
          lines_added: 0,
          lines_removed: 0,
        },
      ],
    });

    expect(screen.queryByText(/^\+/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^−/)).not.toBeInTheDocument();
  });
});

describe("FlatFileList canonical row actions", () => {
  it("copies the root-relative Changes path from the shared actions menu", async () => {
    const user = userEvent.setup();
    const path = "nested/same name # % ' Ω.txt";
    renderList({
      files: [
        {
          path,
          name: path.split("/").at(-1) ?? path,
          status: "modified",
          bytes: 12,
          modified_at: 1_700_000_000,
          lines_added: null,
          lines_removed: null,
        },
      ],
    });

    fireEvent.contextMenu(screen.getByText(path.split("/").at(-1) ?? path));
    await user.click(await screen.findByRole("menuitem", { name: "Copy relative path" }));
    expect(copyTextMock).toHaveBeenCalledWith(path);
  });
});

describe("FlatFileList copy path", () => {
  it("copies the full path even though the button is named for the file", () => {
    // The accessible name carries only the basename (so a screen reader isn't
    // read a whole path per row, and the name can't collide with the folder
    // toggles that legitimately contain directory segments) -- but the
    // CLIPBOARD must get the complete path, which is the whole point.
    renderList({
      files: [
        {
          path: "src/deep/app.ts",
          name: "app.ts",
          status: "modified",
          bytes: 2048,
          modified_at: null,
          lines_added: null,
          lines_removed: null,
        },
      ],
    });

    const button = screen.getByRole("button", { name: "Copy path: app.ts" });
    fireEvent.click(button);

    expect(copyTextMock).toHaveBeenCalledWith("src/deep/app.ts");
  });
});

describe("FlatFileList repository groups", () => {
  const file = (path: string, repository_root?: string): WorkspaceChangedFile => ({
    path,
    name: path.split("/").at(-1)!,
    repository_root,
    status: "modified",
    bytes: 10,
    modified_at: null,
    lines_added: 1,
    lines_removed: 0,
  });
  const files = [file("web/src/app.ts", "web"), file("api/src/app.ts", "api"), file("notes.md")];

  it("groups repositories in order and selects/copies duplicate names by full path", () => {
    const onFileSelect = vi.fn();
    renderList({ files, onFileSelect });
    expect(screen.getAllByRole("group").map((group) => group.getAttribute("aria-label"))).toEqual([
      "api",
      "web",
      "Other files",
    ]);
    for (const root of ["api", "web"]) {
      const group = within(screen.getByRole("group", { name: root }));
      expect(group.getByText("src")).toBeInTheDocument();
      fireEvent.click(group.getByRole("button", { name: `${root}/src/app.ts` }));
      expect(onFileSelect).toHaveBeenLastCalledWith(`${root}/src/app.ts`);
      fireEvent.click(group.getByRole("button", { name: "Copy path: app.ts" }));
      expect(copyTextMock).toHaveBeenLastCalledWith(`${root}/src/app.ts`);
    }
    const header = screen.getByRole("button", { name: "api, 1 changed files" });
    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: "api/src/app.ts" })).toBeNull();
    expect(screen.getByRole("button", { name: "web/src/app.ts" })).toBeInTheDocument();
    fireEvent.click(header);
    expect(screen.getByRole("button", { name: "api/src/app.ts" })).toBeInTheDocument();
  });

  it("keeps search and hidden-file filtering inside groups", () => {
    const onShowHidden = vi.fn();
    renderList({ files: [...files, file("api/.env", "api")], searchQuery: "api/", onShowHidden });
    expect(screen.getAllByRole("group")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "api, 1 changed files" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "api/.env" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Click to show" }));
    expect(onShowHidden).toHaveBeenCalledOnce();
  });

  it("shows hidden files when requested and leaves legacy rows flat", () => {
    renderList({ files: [file(".env"), file("z.py")], showHidden: true });
    expect(screen.queryByRole("group")).toBeNull();
    expect(screen.getByRole("button", { name: ".env" })).toBeInTheDocument();
  });

  it("allows opening deleted files without offering a download", () => {
    const onFileSelect = vi.fn();
    renderList({ files: [{ ...file("api/gone.py", "api"), status: "deleted" }], onFileSelect });
    fireEvent.click(screen.getByRole("button", { name: "api/gone.py" }));
    expect(onFileSelect).toHaveBeenCalledWith("api/gone.py");
    expect(screen.queryByRole("button", { name: /download/i })).toBeNull();
  });
});
