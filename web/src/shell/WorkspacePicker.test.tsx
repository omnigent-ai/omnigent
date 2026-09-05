// Tests for WorkspacePicker.
//
// Two layers:
//   1. The pure path helpers (parentOf / normalizeTypedPath /
//      basename) that drive navigation and the selection label.
//   2. The path-bar behaviour — navigation must mirror into the bar,
//      but a late-arriving listing (home resolving) must NOT clobber
//      what the user is typing.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  basename,
  HostWorkspacePicker,
  isNavigablePath,
  joinPath,
  normalizeTypedPath,
  parentOf,
  WorkspacePicker,
} from "./WorkspacePicker";
import { setHostDefaultWorkspace, useHosts } from "@/hooks/useHosts";
import {
  useCreateHostDirectory,
  useHostFilesystem,
  useHostFilesystemRoots,
  type HostFilesystemEntry,
} from "@/hooks/useHostFilesystem";

vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: vi.fn(),
  useHostFilesystemRoots: vi.fn(() => ({
    data: undefined,
    isLoading: false,
    isPlaceholderData: false,
    error: null,
  })),
  // Default to an idle mutation; tests that exercise creation override
  // mutateAsync. The component only reads this when the new-folder form
  // is open, so the default is harmless for the other suites.
  useCreateHostDirectory: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  setHostDefaultWorkspace: vi.fn(),
}));

const useHostFilesystemMock = vi.mocked(useHostFilesystem);
const useHostFilesystemRootsMock = vi.mocked(useHostFilesystemRoots);
const useCreateHostDirectoryMock = vi.mocked(useCreateHostDirectory);
const useHostsMock = vi.mocked(useHosts);
const setHostDefaultWorkspaceMock = vi.mocked(setHostDefaultWorkspace);

beforeEach(() => {
  useHostFilesystemRootsMock.mockReturnValue(
    result({ data: undefined, isLoading: false, isPlaceholderData: false, error: null }),
  );
  useHostsMock.mockReturnValue({ data: [] } as unknown as ReturnType<typeof useHosts>);
  setHostDefaultWorkspaceMock.mockReset();
  setHostDefaultWorkspaceMock.mockResolvedValue(undefined);
});

describe("HostWorkspacePicker shared preference wiring", () => {
  it("shows and updates the selected Host's pinned folder in every entry point", async () => {
    useHostFilesystemMock.mockReturnValue(
      result({
        data: {
          entries: [dir("Projects", "D:\\Projects")],
          truncated: false,
        },
        isLoading: false,
        isPlaceholderData: false,
      }),
    );
    useHostsMock.mockReturnValue({
      data: [
        {
          host_id: "win_host",
          name: "Windows desktop",
          owner: "me",
          status: "online",
          default_workspace: "D:\\Projects",
        },
      ],
    } as ReturnType<typeof useHosts>);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={client}>
        <HostWorkspacePicker hostId="win_host" initialPath={"D:\\Projects"} />
      </QueryClientProvider>,
    );

    const pin = screen.getByTestId("workspace-picker-default");
    expect(screen.getByTestId("workspace-picker-path-input")).toHaveValue("D:\\Projects");
    expect(screen.queryByTestId("workspace-picker-open-pinned")).not.toBeInTheDocument();
    expect(pin).toHaveAttribute(
      "aria-label",
      "Unpin this folder for Windows desktop. Pinning only provides quick access; new sessions remember the last working folder.",
    );
    fireEvent.click(pin);
    await waitFor(() => expect(setHostDefaultWorkspaceMock).toHaveBeenCalledWith("win_host", null));
  });
});

function dir(name: string, path: string): HostFilesystemEntry {
  return { name, path, type: "directory", bytes: null, modified_at: 0 };
}

interface FakeListing {
  data?: { entries: HostFilesystemEntry[]; truncated: boolean };
  isLoading: boolean;
  isPlaceholderData: boolean;
  error?: null;
}

/** Cast a minimal query result to the hook's return type. */
function result(value: FakeListing): ReturnType<typeof useHostFilesystem> {
  return value as unknown as ReturnType<typeof useHostFilesystem>;
}

describe("parentOf", () => {
  it("returns null at the home view (empty path)", () => {
    // The "home" view has no parent — clicking "up" makes no
    // sense here. The picker hides the up button when null.
    expect(parentOf("")).toBeNull();
  });

  it("returns null at the filesystem root", () => {
    // "/" has no parent. If we returned "" here, the up button
    // would silently bounce the user to home (a different state)
    // rather than disabling.
    expect(parentOf("/")).toBeNull();
  });

  it("returns root for top-level dirs", () => {
    // "/Users" → "/" so the user can climb to the root.
    expect(parentOf("/Users")).toBe("/");
  });

  it("strips one segment from a nested path", () => {
    expect(parentOf("/Users/corey/projects")).toBe("/Users/corey");
    expect(parentOf("/Users/corey")).toBe("/Users");
  });

  it("ignores a trailing slash on the input", () => {
    // A user-typed path with a trailing slash should still
    // climb correctly; without the strip the parent would
    // wrongly include the trailing-empty segment.
    expect(parentOf("/Users/corey/")).toBe("/Users");
  });

  it("climbs Windows drive paths without losing the drive", () => {
    expect(parentOf("D:\\Projects\\Omnigent")).toBe("D:\\Projects");
    expect(parentOf("D:\\Projects")).toBe("D:\\");
    expect(parentOf("D:\\")).toBeNull();
  });
});

describe("normalizeTypedPath", () => {
  it("returns the path unchanged for a clean absolute path", () => {
    expect(normalizeTypedPath("/Users/corey/projects")).toBe("/Users/corey/projects");
  });

  it("trims whitespace", () => {
    // Clipboard pastes pick up surrounding spaces; without
    // trimming, "  /Users  " would fail the leading-slash check.
    expect(normalizeTypedPath("  /Users/corey  ")).toBe("/Users/corey");
  });

  it("collapses runs of slashes", () => {
    // A typo like "/Users//corey" should still navigate to the
    // intended directory rather than failing the listing.
    expect(normalizeTypedPath("/Users//corey///foo")).toBe("/Users/corey/foo");
  });

  it("strips a trailing slash", () => {
    // Trailing slash would break breadcrumb / parent calc, which
    // both assume no trailing separator.
    expect(normalizeTypedPath("/Users/corey/")).toBe("/Users/corey");
  });

  it("preserves the root path exactly", () => {
    // "/" is the only place where a trailing slash is valid; it
    // must round-trip so the user can navigate back to root.
    expect(normalizeTypedPath("/")).toBe("/");
  });

  it("returns null for empty input", () => {
    // Empty input means "I'm clearing the field, don't navigate
    // anywhere" — the caller snaps the input back to the current
    // path rather than nuking the listing.
    expect(normalizeTypedPath("")).toBeNull();
    expect(normalizeTypedPath("   ")).toBeNull();
  });

  it("returns null for relative paths", () => {
    // The host endpoint requires absolute paths. Returning null
    // for relatives keeps the existing listing in place rather
    // than silently 4xx'ing the user.
    expect(normalizeTypedPath("projects/myapp")).toBeNull();
    expect(normalizeTypedPath("./myapp")).toBeNull();
    expect(normalizeTypedPath("../myapp")).toBeNull();
  });

  it("returns null for tilde-prefixed paths when home is unresolved", () => {
    // Before the picker resolves the host's home dir from the
    // first listing response, we can't expand "~". Returning
    // null prevents sending a request that the server would 400.
    expect(normalizeTypedPath("~/projects")).toBeNull();
    expect(normalizeTypedPath("~")).toBeNull();
  });

  it("expands a tilde-prefixed path against the resolved home", () => {
    // The user from the bug report typed "~/omnigent"
    // and nothing happened. Now the picker expands it
    // client-side using the resolved home dir.
    expect(normalizeTypedPath("~/omnigent", "/Users/corey")).toBe("/Users/corey/omnigent");
  });

  it("expands a Windows tilde-relative path using the home path style", () => {
    expect(normalizeTypedPath("~\\Projects", "C:\\Users\\demo")).toBe("C:\\Users\\demo\\Projects");
  });

  it("expands a bare tilde to the resolved home", () => {
    expect(normalizeTypedPath("~", "/Users/corey")).toBe("/Users/corey");
  });

  it("collapses extra slashes after tilde expansion", () => {
    // ~//foo → home + "/" + "/foo" → run-of-slashes collapse.
    expect(normalizeTypedPath("~//projects", "/Users/corey")).toBe("/Users/corey/projects");
  });

  it("strips a trailing slash after tilde expansion", () => {
    expect(normalizeTypedPath("~/projects/", "/Users/corey")).toBe("/Users/corey/projects");
  });

  it("does not support ~user form", () => {
    // ~root, ~alice, etc. would require a server round-trip to
    // resolve. Out of scope for v1 — fall through to "invalid".
    expect(normalizeTypedPath("~root/foo", "/Users/corey")).toBeNull();
  });

  it("preserves and normalizes absolute Windows paths", () => {
    expect(normalizeTypedPath("  d:\\Projects\\Omnigent\\  ")).toBe("D:\\Projects\\Omnigent");
    expect(normalizeTypedPath("E:/Projects//Omnigent/")).toBe("E:\\Projects\\Omnigent");
    expect(normalizeTypedPath("\\\\server\\share\\repo\\")).toBe("\\\\server\\share\\repo");
  });
});

describe("isNavigablePath", () => {
  it.each(["C:\\", "D:\\Projects", "E:/Projects", "\\\\server\\share\\repo"])(
    "accepts Windows absolute path %s",
    (path) => expect(isNavigablePath(path)).toBe(true),
  );

  it("accepts both Windows and POSIX home-relative separators", () => {
    expect(isNavigablePath("~/Projects")).toBe(true);
    expect(isNavigablePath("~\\Projects")).toBe(true);
  });
});

describe("basename", () => {
  it("returns ~ for the empty (pre-resolution home) path", () => {
    // The "Select current" label shows "~" until the listing
    // resolves home to an absolute path.
    expect(basename("")).toBe("~");
  });

  it("returns / for the filesystem root", () => {
    // Root has no trailing segment; without the special case the
    // split would yield "" and the label would read "Select
    // current: " with nothing after it.
    expect(basename("/")).toBe("/");
  });

  it("returns the last segment of a nested path", () => {
    expect(basename("/Users/corey/projects")).toBe("projects");
    expect(basename("/Users")).toBe("Users");
  });

  it("ignores a trailing slash", () => {
    // Filtering out empty segments means a trailing slash doesn't
    // produce an empty basename.
    expect(basename("/Users/corey/")).toBe("corey");
  });

  it("returns the final Windows segment and drive root", () => {
    expect(basename("D:\\Projects\\Omnigent")).toBe("Omnigent");
    expect(basename("D:\\")).toBe("D:\\");
  });

  it("keeps a backslash inside a POSIX filename", () => {
    expect(basename("/tmp/a\\b")).toBe("a\\b");
  });
});

describe("joinPath", () => {
  it("uses the native separator for Windows directories", () => {
    expect(joinPath("D:\\Projects", "new-app")).toBe("D:\\Projects\\new-app");
    expect(joinPath("E:\\", "new-app")).toBe("E:\\new-app");
  });
});

describe("WorkspacePicker path bar", () => {
  beforeEach(() => {
    useHostFilesystemMock.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps in-progress typing when the home dir resolves later", () => {
    // The race: the user types before the home listing returns.
    // The late resolve changes currentAbsolute, which used to
    // overwrite the path bar mid-edit.
    let listing: FakeListing = {
      data: undefined,
      isLoading: true,
      isPlaceholderData: false,
    };
    useHostFilesystemMock.mockImplementation(() => result(listing));

    const { rerender } = render(<WorkspacePicker hostId="host_1" />);
    const input = screen.getByTestId("workspace-picker-path-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "/Users/serena.ruan/Doc" } });

    // Home listing arrives — resolvedHome derives, currentAbsolute flips.
    listing = {
      data: {
        entries: [dir("x", "/Users/serena.ruan/x")],
        truncated: false,
      },
      isLoading: false,
      isPlaceholderData: false,
    };
    rerender(<WorkspacePicker hostId="host_1" />);

    expect(input.value).toBe("/Users/serena.ruan/Doc");
  });

  it("mirrors the path into the bar when navigating into a folder", () => {
    // The fix must not break normal mirroring: clicking a row
    // supersedes any typing and refills the bar from the listing.
    useHostFilesystemMock.mockReturnValue(
      result({
        data: {
          entries: [dir("projects", "/Users/serena.ruan/projects")],
          truncated: false,
        },
        isLoading: false,
        isPlaceholderData: false,
      }),
    );

    render(<WorkspacePicker hostId="host_1" supportsFilesystemRoots />);
    const input = screen.getByTestId("workspace-picker-path-input") as HTMLInputElement;
    fireEvent.click(screen.getByTestId("workspace-picker-entry-projects"));
    expect(input.value).toBe("/Users/serena.ruan/projects");
  });

  it("explains that the address bar accepts an absolute path", () => {
    useHostFilesystemMock.mockReturnValue(
      result({ data: undefined, isLoading: false, isPlaceholderData: false }),
    );
    render(<WorkspacePicker hostId="host_1" initialPath="/x" />);
    expect(screen.getByTestId("workspace-picker-path-input")).toHaveAttribute(
      "aria-label",
      "Folder path. Type an absolute path and press Enter to open it.",
    );
  });

  it("resolves a tilde start path to an absolute one for selection", () => {
    // Opening at "~/projects" (the host expands ~): the listing's
    // entries come back absolute, so "Select current" must return the
    // real absolute dir, not the literal "~/projects" we started at.
    useHostFilesystemMock.mockReturnValue(
      result({
        data: {
          entries: [dir("app", "/Users/corey/projects/app")],
          truncated: false,
        },
        isLoading: false,
        isPlaceholderData: false,
      }),
    );
    const onSelect = vi.fn();
    render(<WorkspacePicker hostId="host_1" initialPath="~/projects" onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId("workspace-picker-select"));
    expect(onSelect).toHaveBeenCalledWith("/Users/corey/projects");
  });
});

describe("WorkspacePicker filesystem roots", () => {
  it("shows Windows drives and enters a selected drive", () => {
    useHostFilesystemMock.mockReturnValue(
      result({ data: undefined, isLoading: false, isPlaceholderData: false, error: null }),
    );
    useHostFilesystemRootsMock.mockReturnValue(
      result({
        data: {
          entries: [dir("C:\\", "C:\\"), dir("D:\\", "D:\\")],
          truncated: false,
        },
        isLoading: false,
        isPlaceholderData: false,
        error: null,
      }),
    );

    render(<WorkspacePicker hostId="host_1" supportsFilesystemRoots />);
    fireEvent.click(screen.getByTestId("workspace-picker-roots"));
    fireEvent.click(screen.getByTestId("workspace-picker-entry-D:\\"));

    expect(useHostFilesystemMock).toHaveBeenCalledWith("host_1", "D:\\");
  });

  it("keeps root browsing hidden for an older Host", () => {
    useHostFilesystemMock.mockReturnValue(
      result({ data: undefined, isLoading: false, isPlaceholderData: false, error: null }),
    );

    render(<WorkspacePicker hostId="old_host" />);

    expect(screen.queryByTestId("workspace-picker-roots")).not.toBeInTheDocument();
    expect(useHostFilesystemRootsMock).toHaveBeenCalledWith("old_host", false);
  });
});

describe("WorkspacePicker pinned folder", () => {
  it("opens the pinned shortcut without changing the selected working folder", () => {
    useHostFilesystemMock.mockReturnValue(
      result({ data: undefined, isLoading: false, isPlaceholderData: false }),
    );
    const onSelect = vi.fn();
    render(
      <WorkspacePicker
        hostId="host_1"
        initialPath="/work/current"
        defaultPath="/work/pinned"
        onSelect={onSelect}
      />,
    );
    expect(useHostFilesystemMock).toHaveBeenCalledWith("host_1", "/work/current");
    fireEvent.click(screen.getByTestId("workspace-picker-open-pinned"));
    expect(useHostFilesystemMock).toHaveBeenCalledWith("host_1", "/work/pinned");
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("keeps pinned folders as shortcuts when the Host changes", () => {
    useHostFilesystemMock.mockReturnValue(
      result({ data: undefined, isLoading: false, isPlaceholderData: false, error: null }),
    );

    const { rerender } = render(
      <WorkspacePicker hostId="mac_host" defaultPath="/Users/me/Projects" />,
    );
    expect(useHostFilesystemMock).toHaveBeenCalledWith("mac_host", "");

    rerender(<WorkspacePicker hostId="windows_host" defaultPath={"D:\\Projects"} />);
    expect(useHostFilesystemMock).toHaveBeenCalledWith("windows_host", "");
    expect(screen.getByTestId("workspace-picker-open-pinned")).toHaveAttribute(
      "aria-label",
      "Open pinned folder: D:\\Projects",
    );
  });

  it("pins the current Windows directory for quick access", () => {
    const onDefaultPathChange = vi.fn();
    useHostFilesystemMock.mockReturnValue(
      result({
        data: { entries: [dir("Omnigent", "D:\\Projects\\Omnigent")], truncated: false },
        isLoading: false,
        isPlaceholderData: false,
        error: null,
      }),
    );

    render(
      <WorkspacePicker
        hostId="host_1"
        initialPath={"D:\\Projects"}
        defaultPathHostName="Windows desktop"
        onDefaultPathChange={onDefaultPathChange}
      />,
    );
    expect(screen.getByTestId("workspace-picker-default")).toHaveAttribute(
      "aria-label",
      "Pin this folder for quick access on Windows desktop. New sessions remember the last working folder.",
    );
    fireEvent.click(screen.getByTestId("workspace-picker-default"));

    expect(onDefaultPathChange).toHaveBeenCalledWith("D:\\Projects");
  });
});

// The conflict banner warns when other live agents already work in the
// directory currently being browsed. Occupancy is supplied by the caller
// (occupancyForPath), keyed on the picker's current absolute path.
describe("WorkspacePicker conflict banner", () => {
  beforeEach(() => {
    useHostFilesystemMock.mockReset();
    // The banner is independent of the listing; an empty one keeps the
    // picker rendering without dictating the current directory.
    useHostFilesystemMock.mockReturnValue(
      result({ data: undefined, isLoading: false, isPlaceholderData: false }),
    );
  });

  afterEach(() => {
    cleanup();
  });

  it("shows the banner for the current directory, querying it by absolute path", () => {
    const occupancyForPath = vi.fn((abs: string) => (abs === "/Users/corey/repo" ? 2 : 0));
    render(
      <WorkspacePicker
        hostId="host_1"
        initialPath="/Users/corey/repo"
        occupancyForPath={occupancyForPath}
      />,
    );
    // It's the browsed directory (not, say, home) that's queried — so the
    // warning tracks the folder you'd actually commit to.
    expect(occupancyForPath).toHaveBeenCalledWith("/Users/corey/repo");
    // The count (2) flows into the copy, proving it's the callback's return
    // value driving the banner, not a hardcoded string.
    expect(screen.getByTestId("workspace-picker-conflict").textContent).toContain(
      "2 other agents are",
    );
  });

  it("hides the banner when the current directory is unoccupied", () => {
    // occupancyForPath returns 0 → no live agent here → no banner.
    render(
      <WorkspacePicker
        hostId="host_1"
        initialPath="/Users/corey/repo"
        occupancyForPath={() => 0}
      />,
    );
    expect(screen.queryByTestId("workspace-picker-conflict")).toBeNull();
  });

  it("renders no banner when occupancyForPath is omitted", () => {
    // The prop is optional; without it the picker never warns (the
    // non-conflict-aware callers, e.g. ResumeWithDirectoryDialog).
    render(<WorkspacePicker hostId="host_1" initialPath="/Users/corey/repo" />);
    expect(screen.queryByTestId("workspace-picker-conflict")).toBeNull();
  });
});

// Live selection: onNavigate reports the current directory continuously
// (mount + every navigation) so a caller can update its value without a
// "Select" button. Passing only onNavigate (no onSelect) also hides the
// button — the new-session landing flow's mode.
describe("WorkspacePicker live selection (onNavigate)", () => {
  beforeEach(() => {
    useHostFilesystemMock.mockReset();
    useHostFilesystemMock.mockReturnValue(
      result({
        data: { entries: [dir("src", "/x/src")], truncated: false },
        isLoading: false,
        isPlaceholderData: false,
      }),
    );
  });

  afterEach(() => {
    cleanup();
  });

  it("reports the opened directory on mount and the new one after navigating", () => {
    const onNavigate = vi.fn();
    render(<WorkspacePicker hostId="host_1" initialPath="/x" onNavigate={onNavigate} />);
    // Mount seeds the value with the directory the picker opened at, so the
    // common case (open → it's already what you want) needs zero clicks.
    expect(onNavigate).toHaveBeenCalledWith("/x");
    // Clicking a folder navigates into it AND reports it as the new value.
    fireEvent.click(screen.getByTestId("workspace-picker-entry-src"));
    expect(onNavigate).toHaveBeenLastCalledWith("/x/src");
  });

  it("hides the Select button when onSelect is not supplied", () => {
    // The live-update callers drop the explicit commit button entirely.
    render(<WorkspacePicker hostId="host_1" initialPath="/x" onNavigate={vi.fn()} />);
    expect(screen.queryByTestId("workspace-picker-select")).toBeNull();
  });
});

// Folder search is a separate right-side control so the address bar always
// means exact-path navigation. It filters only the current level because the
// Host API does not provide a recursive machine-wide search index.
describe("WorkspacePicker folder search", () => {
  beforeEach(() => {
    useHostFilesystemMock.mockReset();
    useHostFilesystemMock.mockReturnValue(
      result({
        data: {
          entries: [dir("src", "/x/src"), dir("styles", "/x/styles"), dir("docs", "/x/docs")],
          truncated: false,
        },
        isLoading: false,
        isPlaceholderData: false,
        // The real hook reports error: null on success; the no-match empty
        // state is gated on `error === null`, so set it explicitly.
        error: null,
      }),
    );
  });

  afterEach(() => {
    cleanup();
  });

  it("finds current-level folders by a case-insensitive name fragment", () => {
    render(<WorkspacePicker hostId="host_1" initialPath="/x" />);
    // All three show before search opens.
    expect(screen.getByTestId("workspace-picker-entry-src")).toBeTruthy();
    expect(screen.getByTestId("workspace-picker-entry-docs")).toBeTruthy();
    fireEvent.change(screen.getByTestId("workspace-picker-search-input"), {
      // Substring rather than prefix: Finder-style filtering finds "styles".
      target: { value: "YLE" },
    });
    expect(screen.getByTestId("workspace-picker-entry-styles")).toBeTruthy();
    expect(screen.queryByTestId("workspace-picker-entry-src")).toBeNull();
    expect(screen.queryByTestId("workspace-picker-entry-docs")).toBeNull();
  });

  it("shows a scoped no-matches message", () => {
    render(<WorkspacePicker hostId="host_1" initialPath="/x" />);
    fireEvent.change(screen.getByTestId("workspace-picker-search-input"), {
      target: { value: "zzz" },
    });
    expect(screen.getByText("No matching folders in this directory")).toBeTruthy();
    expect(screen.queryByTestId("workspace-picker-entry-src")).toBeNull();
  });
});

describe("joinPath", () => {
  it("joins a nested directory and a child name", () => {
    expect(joinPath("/Users/me", "new-app")).toBe("/Users/me/new-app");
  });

  it("does not double the slash at the filesystem root", () => {
    // "/" + "foo" must be "/foo", not "//foo" — the latter would
    // confuse the host's path resolution.
    expect(joinPath("/", "foo")).toBe("/foo");
  });

  it("ignores a trailing slash on the parent", () => {
    expect(joinPath("/Users/me/", "foo")).toBe("/Users/me/foo");
  });

  it("trims surrounding whitespace from the child name", () => {
    expect(joinPath("/Users/me", "  foo  ")).toBe("/Users/me/foo");
  });
});

// The "New folder" action lets a user create a directory inline rather
// than dropping to a terminal. It only makes sense once the picker has
// resolved a real absolute directory to create in.
describe("WorkspacePicker new folder", () => {
  beforeEach(() => {
    useHostFilesystemMock.mockReset();
    useCreateHostDirectoryMock.mockReset();
    useCreateHostDirectoryMock.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof useCreateHostDirectory>);
  });

  afterEach(() => {
    cleanup();
  });

  function listingWith(entries: HostFilesystemEntry[]) {
    useHostFilesystemMock.mockReturnValue(
      result({ data: { entries, truncated: false }, isLoading: false, isPlaceholderData: false }),
    );
  }

  it("creates a folder under the current directory and navigates into it", async () => {
    listingWith([dir("app", "/Users/corey/projects/app")]);
    const mutateAsync = vi.fn().mockResolvedValue("/Users/corey/projects/fresh");
    useCreateHostDirectoryMock.mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useCreateHostDirectory>);

    render(<WorkspacePicker hostId="host_1" initialPath="/Users/corey/projects" />);

    fireEvent.click(screen.getByTestId("workspace-picker-new-folder"));
    fireEvent.change(screen.getByTestId("workspace-picker-new-folder-input"), {
      target: { value: "fresh" },
    });
    fireEvent.click(screen.getByTestId("workspace-picker-new-folder-create"));

    await Promise.resolve();
    expect(mutateAsync).toHaveBeenCalledWith({
      hostId: "host_1",
      path: "/Users/corey/projects/fresh",
    });
  });

  it("shows the server error inline when creation fails", async () => {
    listingWith([dir("app", "/Users/corey/projects/app")]);
    const mutateAsync = vi.fn().mockRejectedValue(new Error("directory already exists"));
    useCreateHostDirectoryMock.mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useCreateHostDirectory>);

    render(<WorkspacePicker hostId="host_1" initialPath="/Users/corey/projects" />);

    fireEvent.click(screen.getByTestId("workspace-picker-new-folder"));
    fireEvent.change(screen.getByTestId("workspace-picker-new-folder-input"), {
      target: { value: "app" },
    });
    fireEvent.click(screen.getByTestId("workspace-picker-new-folder-create"));

    // Let the rejected mutation settle and the error state render.
    await screen.findByTestId("workspace-picker-new-folder-error");
    expect(screen.getByTestId("workspace-picker-new-folder-error").textContent).toContain(
      "already exists",
    );
  });

  it("disables the New folder button until an absolute directory resolves", () => {
    // Home view ("") with no listing yet — currentAbsolute is "", so the
    // button is disabled (there is no real directory to create in).
    useHostFilesystemMock.mockReturnValue(
      result({ data: undefined, isLoading: true, isPlaceholderData: false }),
    );
    render(<WorkspacePicker hostId="host_1" />);
    const btn = screen.getByTestId("workspace-picker-new-folder") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("creates under ~ when home is empty (no entry to resolve the absolute path)", async () => {
    // An empty home has no entries, so the absolute home path can't be
    // derived — but the listing HAS loaded. The button must still enable
    // and create under "~" (the host expands it), otherwise the first
    // folder in an empty home could never be made.
    listingWith([]);
    const mutateAsync = vi.fn().mockResolvedValue("/home/e2e/fresh");
    useCreateHostDirectoryMock.mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useCreateHostDirectory>);

    render(<WorkspacePicker hostId="host_1" />);

    const btn = screen.getByTestId("workspace-picker-new-folder") as HTMLButtonElement;
    expect(btn.disabled).toBe(false);

    fireEvent.click(btn);
    fireEvent.change(screen.getByTestId("workspace-picker-new-folder-input"), {
      target: { value: "fresh" },
    });
    fireEvent.click(screen.getByTestId("workspace-picker-new-folder-create"));

    await Promise.resolve();
    expect(mutateAsync).toHaveBeenCalledWith({ hostId: "host_1", path: "~/fresh" });
  });
});

describe("WorkspacePicker back-to-workspace", () => {
  it("is absent when no workspace is supplied", () => {
    // The new-session / fork / project dialogs are *choosing* a workspace, so
    // there is nothing to go back to and Home (~) is the only anchor. This is
    // why the button is its own control rather than a repurposed Home.
    useHostFilesystemMock.mockReturnValue(
      result({
        data: { entries: [dir("app", "/Users/corey/projects/app")], truncated: false },
        isLoading: false,
        isPlaceholderData: false,
      }),
    );

    render(<WorkspacePicker hostId="host_1" />);

    expect(screen.queryByTestId("workspace-picker-workspace")).toBeNull();
    expect(screen.getByTestId("workspace-picker-home")).toBeInTheDocument();
  });

  it("returns to the workspace in one click after wandering off", () => {
    useHostFilesystemMock.mockReturnValue(
      result({
        data: { entries: [dir("Music", "/Users/corey/Music")], truncated: false },
        isLoading: false,
        isPlaceholderData: false,
      }),
    );
    const onNavigate = vi.fn();

    render(
      <WorkspacePicker
        hostId="host_1"
        initialPath="/Users/corey"
        workspacePath="/Users/corey/repo"
        onNavigate={onNavigate}
      />,
    );
    fireEvent.click(screen.getByTestId("workspace-picker-workspace"));

    expect(onNavigate).toHaveBeenCalledWith("/Users/corey/repo");
  });

  it("is disabled once the workspace is already showing", () => {
    // Same treatment as Up at the filesystem root: kept in place but inert,
    // so the header does not reflow as the user navigates.
    useHostFilesystemMock.mockReturnValue(
      result({
        data: { entries: [dir("src", "/Users/corey/repo/src")], truncated: false },
        isLoading: false,
        isPlaceholderData: false,
      }),
    );

    render(
      <WorkspacePicker
        hostId="host_1"
        initialPath="/Users/corey/repo"
        workspacePath="/Users/corey/repo"
      />,
    );

    expect(screen.getByTestId("workspace-picker-workspace")).toBeDisabled();
  });
});

// The picker opens inside popovers and dialogs, which focus their first
// tabbable child — the header's Up button. That focus must not reveal its
// tooltip, or merely opening the picker throws a black label over the listing.
describe("WorkspacePicker header tooltips", () => {
  beforeEach(() => {
    useHostFilesystemMock.mockReset();
    useHostFilesystemMock.mockReturnValue(
      result({
        data: { entries: [dir("src", "/Users/corey/repo/src")], truncated: false },
        isLoading: false,
        isPlaceholderData: false,
      }),
    );
  });

  afterEach(() => {
    cleanup();
  });

  it("stays hidden when opening the picker focuses the Up button", async () => {
    render(
      <Popover>
        <PopoverTrigger data-testid="open-picker">Working folder</PopoverTrigger>
        <PopoverContent>
          <WorkspacePicker hostId="host_1" initialPath="/Users/corey/repo" />
        </PopoverContent>
      </Popover>,
    );

    fireEvent.click(screen.getByTestId("open-picker"));
    const up = await screen.findByTestId("workspace-picker-up");
    // Radix's own autofocus is what used to trip the tooltip; assert it landed
    // so the test would notice if the focus behaviour changed instead.
    expect(up).toHaveFocus();
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});
