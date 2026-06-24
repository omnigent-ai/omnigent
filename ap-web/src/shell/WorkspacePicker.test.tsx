// Tests for WorkspacePicker.
//
// Two layers:
//   1. The pure path helpers (parentOf / normalizeTypedPath /
//      basename) that drive navigation and the selection label.
//   2. The path-bar behaviour — navigation must mirror into the bar,
//      but a late-arriving listing (home resolving) must NOT clobber
//      what the user is typing.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  basename,
  joinPath,
  listingFilter,
  normalizeTypedPath,
  parentOf,
  WorkspacePicker,
} from "./WorkspacePicker";
import {
  useCreateHostDirectory,
  useHostFilesystem,
  type HostFilesystemEntry,
} from "@/hooks/useHostFilesystem";
import i18n from "@/i18n";

// Resolve expected copy through the app's own i18n instance (jsdom → en),
// so assertions track translation keys rather than hardcoded English and a
// missing French key surfaces as a parity failure, not a green test.
const t = i18n.getFixedT(null, "nav");

vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: vi.fn(),
  // Default to an idle mutation; tests that exercise creation override
  // mutateAsync. The component only reads this when the new-folder form
  // is open, so the default is harmless for the other suites.
  useCreateHostDirectory: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
}));

const useHostFilesystemMock = vi.mocked(useHostFilesystem);
const useCreateHostDirectoryMock = vi.mocked(useCreateHostDirectory);

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
});

// listingFilter decides whether (and how) the path-bar text narrows the
// current directory's listing. The table pins both the cases that DO filter
// (a bare fragment, or a trailing segment under the current dir, incl. ~ and
// root) and the cases that must NOT — blank, exactly the current path, or a
// path into a different directory (which is navigation, not a filter). A
// false positive here would hide entries while the user is navigating away.
describe("listingFilter", () => {
  it.each<[string, string, string | null, string | null]>([
    // [pathInput, currentAbsolute, home, expected]
    // Bare fragment, no slash → filters the current directory by it.
    ["pro", "/Users/me", null, "pro"],
    // Trailing segment whose parent IS the current directory → filters.
    ["/Users/me/pro", "/Users/me", null, "pro"],
    // Exactly the current path (the mirrored value) → not a filter.
    ["/Users/me", "/Users/me", null, null],
    // Blank input → no filter.
    ["", "/Users/me", null, null],
    ["   ", "/Users/me", null, null],
    // "<dir>/" with nothing past the slash yet → no filter.
    ["/Users/me/", "/Users/me", null, null],
    // A path into a *different* directory → navigation, not a filter.
    ["/etc", "/Users/me", null, null],
    ["/var/lo", "/Users/me", null, null],
    // ~ expands against home, so "~/pro" filters when home is the current dir.
    ["~/pro", "/Users/me", "/Users/me", "pro"],
    // At the root, "/sr" is a fragment of "/" → filters.
    ["/sr", "/", null, "sr"],
  ])("listingFilter(%j, %j, %j) → %j", (input, current, home, expected) => {
    expect(listingFilter(input, current, home)).toBe(expected);
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

    render(<WorkspacePicker hostId="host_1" />);
    const input = screen.getByTestId("workspace-picker-path-input") as HTMLInputElement;
    fireEvent.click(screen.getByTestId("workspace-picker-entry-projects"));
    expect(input.value).toBe("/Users/serena.ruan/projects");
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
    expect(screen.getByTestId("workspace-picker-conflict").textContent).toBe(
      t("workspacePickerConflict", { count: 2 }),
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

// Filter-as-you-type: typing a fragment in the path bar narrows the current
// directory's listing (the listingFilter unit tests cover the parsing; these
// confirm the wiring through to the rendered rows).
describe("WorkspacePicker listing filter", () => {
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

  it("narrows the listing to entries matching the typed fragment", () => {
    render(<WorkspacePicker hostId="host_1" initialPath="/x" />);
    // All three show before any filter.
    expect(screen.getByTestId("workspace-picker-entry-src")).toBeTruthy();
    expect(screen.getByTestId("workspace-picker-entry-docs")).toBeTruthy();
    // Typing "s" keeps only the names starting with "s".
    fireEvent.change(screen.getByTestId("workspace-picker-path-input"), {
      target: { value: "s" },
    });
    expect(screen.getByTestId("workspace-picker-entry-src")).toBeTruthy();
    expect(screen.getByTestId("workspace-picker-entry-styles")).toBeTruthy();
    expect(screen.queryByTestId("workspace-picker-entry-docs")).toBeNull();
  });

  it("shows a no-matches message when nothing matches the fragment", () => {
    render(<WorkspacePicker hostId="host_1" initialPath="/x" />);
    fireEvent.change(screen.getByTestId("workspace-picker-path-input"), {
      target: { value: "zzz" },
    });
    // Distinct from "(empty directory)" so the user knows it's the filter,
    // not an actually-empty folder.
    expect(screen.getByText(t("workspacePickerNoMatchingEntries"))).toBeTruthy();
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
