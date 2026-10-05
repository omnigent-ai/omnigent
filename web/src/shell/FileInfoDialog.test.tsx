import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type * as WorkspaceChangedFilesHooks from "@/hooks/useWorkspaceChangedFiles";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { afterEach, expect, it, vi } from "vitest";

const { copyTextMock } = vi.hoisted(() => ({ copyTextMock: vi.fn(() => Promise.resolve()) }));
vi.mock("@/lib/clipboard", () => ({ copyText: copyTextMock }));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceChangedFilesHooks>()),
  useWorkspaceAllFiles: () => ({ data: { available: true, data: [] }, isLoading: false }),
  useWorkspaceChangedFiles: () => ({ data: { available: true, data: [] }, isLoading: false }),
  useWorkspaceDirectory: () => ({ data: [], isLoading: false, isError: false }),
  useWorkspaceDirectories: () => new Map(),
  useWorkspaceEnvironment: () => ({
    data: { available: true, root: null, home: null, reachable: null },
    isLoading: false,
    isError: false,
  }),
  useWorkspaceFileSearch: () => ({
    data: { files: [], truncated: false },
    isFetching: false,
    isPlaceholderData: false,
    isLoading: false,
    isError: false,
  }),
}));
vi.mock("@/hooks/useSession", () => ({ useSession: () => ({ session: null }) }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionHostOnline: () => null,
  useSessionRunnerOnline: () => true,
}));
vi.mock("@/components/ui/dropdown-menu", async () => {
  const React = await import("react");
  const passthrough = ({ children }: { children: React.ReactNode }) => children;
  const trigger = React.forwardRef<
    HTMLButtonElement,
    React.ComponentPropsWithoutRef<"button"> & { asChild?: boolean }
  >(function Trigger({ children, asChild, ...props }, ref) {
    if (asChild) return children;
    return (
      <button ref={ref} type="button" {...props}>
        {children}
      </button>
    );
  });
  return {
    DropdownMenu: passthrough,
    DropdownMenuTrigger: trigger,
    DropdownMenuContent: passthrough,
    DropdownMenuRadioGroup: passthrough,
    DropdownMenuRadioItem: passthrough,
  };
});
vi.mock("@/store/chatStore", () => ({
  useChatStore: (
    selector: (state: { conversationId: string | null; sessionStatus: string }) => unknown,
  ) => selector({ conversationId: null, sessionStatus: "ready" }),
}));

import { FilesPanel } from "./FilesPanel";
import { FileInfoDialog } from "./FileInfoDialog";
import { FilesPanelFocusContext } from "./FileRowActions";
import type { FileRowInfo } from "./FileRowActions";

function expectTimezoneName(text: string | null | undefined, modifiedAt: number) {
  const timezoneName = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" })
    .formatToParts(new Date(modifiedAt * 1000))
    .find((part) => part.type === "timeZoneName")?.value;
  expect(timezoneName).toBeTruthy();
  expect(text).toContain(timezoneName);
}

function EscapeDrawer({ info }: { info: FileRowInfo }) {
  const [open, setOpen] = useState(true);
  const [dialogOpen, setDialogOpen] = useState(true);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [returnFocus, setReturnFocus] = useState<HTMLElement | null>(null);

  useLayoutEffect(() => setReturnFocus(triggerRef.current), []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, []);

  return (
    <div data-testid="drawer" data-state={open ? "open" : "closed"}>
      {open && (
        <>
          <button ref={triggerRef} type="button">
            Open info
          </button>
          <FileInfoDialog
            info={dialogOpen ? info : null}
            onOpenChange={setDialogOpen}
            returnFocus={returnFocus}
          />
        </>
      )}
    </div>
  );
}

function DetachedTriggerHarness() {
  const [info, setInfo] = useState<FileRowInfo | null>({
    name: "notes.txt",
    path: "notes.txt",
    kind: "file",
    bytes: 1,
  });
  const [showTrigger, setShowTrigger] = useState(true);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [returnFocus, setReturnFocus] = useState<HTMLElement | null>(null);

  useLayoutEffect(() => setReturnFocus(triggerRef.current), []);

  return (
    <FilesPanelFocusContext.Provider value={panelRef}>
      <nav data-testid="desktop-rail" role="tablist">
        <button type="button" role="tab" aria-selected="true" tabIndex={0}>
          Chat
        </button>
      </nav>
      <aside ref={panelRef} tabIndex={-1} data-testid="files-panel-drawer" data-state="open">
        <button type="button" role="tab" aria-selected="true" tabIndex={0}>
          Files
        </button>
        {showTrigger && (
          <button ref={triggerRef} type="button">
            Open info
          </button>
        )}
        <FileInfoDialog
          info={info}
          onOpenChange={(open) => {
            if (!open) {
              setInfo(null);
              setShowTrigger(false);
            }
          }}
          returnFocus={returnFocus}
        />
      </aside>
    </FilesPanelFocusContext.Provider>
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("shows only available client-side file metadata and copies its canonical path", async () => {
  const user = userEvent.setup();
  const fetchSpy = vi.spyOn(globalThis, "fetch");
  const info: FileRowInfo = {
    name: "same name # Ω.txt",
    path: "deep/same name # Ω.txt",
    kind: "file",
    bytes: null,
    modifiedAt: null,
    status: "deleted",
    linesAdded: null,
    linesRemoved: 3,
    lastKnown: true,
  };
  const onOpenChange = vi.fn();
  render(<FileInfoDialog info={info} onOpenChange={onOpenChange} returnFocus={null} />);

  expect(screen.getByRole("dialog", { name: "File info (last known)" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { level: 3, name: "same name # Ω.txt" })).toBeInTheDocument();
  expect(screen.getByText("deep/same name # Ω.txt")).toBeInTheDocument();
  expect(screen.getByText("Path", { selector: "dt" })).toBeInTheDocument();
  expect(screen.getByText("File (.txt)", { selector: "dd" })).toBeInTheDocument();
  expect(screen.getByText("Deleted · −3 lines")).toBeInTheDocument();
  expect(
    screen.getByText(/With Git, changes compare the workspace with the last commit/),
  ).toBeInTheDocument();
  expect(screen.queryByText("Size", { selector: "dt" })).not.toBeInTheDocument();
  expect(screen.queryByText("Modified", { selector: "dt" })).not.toBeInTheDocument();
  expect(screen.queryByText(/Lines added|Lines removed/)).not.toBeInTheDocument();
  expect(screen.queryByText(/mode|owner|target|mime|children|recursive/i)).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Copy path: same name # Ω.txt" }));
  expect(copyTextMock).toHaveBeenCalledWith("deep/same name # Ω.txt");
  expect(fetchSpy).not.toHaveBeenCalled();
});

it("shows exact bytes, root location, and a timezone on an ordinary file", () => {
  render(
    <FileInfoDialog
      info={{
        name: "app.ts",
        path: "app.ts",
        kind: "file",
        bytes: 231_424,
        modifiedAt: 1_759_608_000,
        status: "modified",
        linesAdded: 12,
        linesRemoved: 3,
      }}
      onOpenChange={vi.fn()}
      returnFocus={null}
    />,
  );
  expect(screen.getByText("Location", { selector: "dt" })).toBeInTheDocument();
  expect(screen.getByText("Workspace root", { selector: "code" })).toBeInTheDocument();
  expect(screen.getByText("TypeScript file (.ts)")).toBeInTheDocument();
  expect(screen.getByText("226 KB (231,424 bytes)")).toBeInTheDocument();
  expect(screen.getByText("Modified · +12 −3 lines")).toBeInTheDocument();
  const modified = screen.getByText("Modified", { selector: "dt" }).nextElementSibling;
  expectTimezoneName(modified?.textContent, 1_759_608_000);
});

it("shows Host path and copy retains the absolute canonical path", async () => {
  const user = userEvent.setup();
  render(
    <FileInfoDialog
      info={{ name: "outside.pdf", path: "/tmp/outside.pdf", kind: "file", bytes: 5 }}
      onOpenChange={vi.fn()}
      returnFocus={null}
    />,
  );
  expect(screen.getByText("Host path", { selector: "dt" })).toBeInTheDocument();
  expect(screen.getByText("PDF document (.pdf)")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Copy path: outside.pdf" }));
  expect(copyTextMock).toHaveBeenCalledWith("/tmp/outside.pdf");
});

it("labels a changed folder without file status wording or counts", () => {
  render(
    <FileInfoDialog
      info={{
        name: "models",
        path: "models",
        kind: "folder",
        modifiedAt: 1_759_608_000,
        status: "created",
        linesAdded: 12,
        linesRemoved: 3,
      }}
      onOpenChange={vi.fn()}
      returnFocus={null}
    />,
  );
  expect(screen.getByRole("dialog", { name: "Folder info" })).toBeInTheDocument();
  expect(screen.getByText("Folder", { selector: "dd" })).toBeInTheDocument();
  expect(screen.getByText("Contains changes")).toBeInTheDocument();
  expect(screen.queryByText(/New file|\+12|−3/)).not.toBeInTheDocument();
  expect(screen.queryByText("Size", { selector: "dt" })).not.toBeInTheDocument();
  const modified = screen.getByText("Modified", { selector: "dt" }).nextElementSibling;
  expectTimezoneName(modified?.textContent, 1_759_608_000);
});

it("uses Not available for metadata without known changes", () => {
  render(
    <FileInfoDialog
      info={{ name: "empty", path: "/tmp/empty", kind: "folder", modifiedAt: null }}
      onOpenChange={vi.fn()}
      returnFocus={null}
    />,
  );
  expect(screen.getByRole("dialog", { name: "Folder info" })).toBeInTheDocument();
  const modifiedLabel = screen.getByText("Modified", { selector: "dt" });
  expect(modifiedLabel.nextElementSibling).toHaveTextContent("Not available");
  expect(screen.getByText("Host path", { selector: "dt" })).toBeInTheDocument();
  expect(screen.queryByText(/size/i)).not.toBeInTheDocument();
});

it("closes Info on Escape without dismissing its containing drawer", async () => {
  const user = userEvent.setup();
  render(<EscapeDrawer info={{ name: "notes.txt", path: "notes.txt", kind: "file", bytes: 1 }} />);

  screen.getByRole("dialog").focus();
  await user.keyboard("{Escape}");

  expect(screen.getByTestId("drawer")).toHaveAttribute("data-state", "open");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open info" })).toHaveFocus();
});

it("returns focus to its Files panel when opener is gone and other tabs are selected", async () => {
  const user = userEvent.setup();
  render(<DetachedTriggerHarness />);

  await user.click(screen.getByRole("button", { name: "Close" }));

  expect(screen.getByTestId("files-panel-drawer")).toHaveFocus();
});

it("gives its focus fallback an accessible name", () => {
  const panel = (flatView: boolean) => (
    <MemoryRouter initialEntries={["/c/files-panel-name"]}>
      <Routes>
        <Route
          path="/c/:conversationId"
          element={
            <FilesPanel
              sort="recent"
              onSortChange={vi.fn()}
              flatView={flatView}
              onFileSelect={vi.fn()}
              showHidden={false}
              onShowHiddenChange={vi.fn()}
            />
          }
        />
      </Routes>
    </MemoryRouter>
  );
  const { rerender } = render(panel(false));

  expect(screen.getByRole("region", { name: "Files" })).toHaveAttribute("tabindex", "-1");

  rerender(panel(true));

  expect(screen.getByRole("region", { name: "Changes" })).toHaveAttribute("tabindex", "-1");
});
