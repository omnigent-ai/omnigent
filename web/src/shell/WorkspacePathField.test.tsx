// Tests for the working-directory combobox.
//
// Two layers:
//   1. splitTypedPath — the pure helper that decides which directory
//      to list and what prefix to filter it by.
//   2. The component itself — opening the dropdown and selecting a
//      row. The interaction tests guard a real regression: when the
//      dropdown was briefly portaled out of the dialog it rendered
//      but its rows could not be clicked (Radix's modal layer) or
//      were clipped by the scrollable form body.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { splitTypedPath, WorkspacePathField } from "./WorkspacePathField";
import { useHostFilesystem, type HostFilesystemEntry } from "@/hooks/useHostFilesystem";

vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: vi.fn(),
}));

const useHostFilesystemMock = vi.mocked(useHostFilesystem);

function dir(name: string, path: string): HostFilesystemEntry {
  return { name, path, type: "directory", bytes: null, modified_at: 0 };
}

/** Stub the hook with a fixed listing for every (host, path). */
function mockListing(
  entries: HostFilesystemEntry[],
  opts: { isLoading?: boolean; truncated?: boolean } = {},
) {
  useHostFilesystemMock.mockReturnValue({
    data: { entries, truncated: opts.truncated ?? false },
    isLoading: opts.isLoading ?? false,
    // The component only reads data/isLoading; the rest of the
    // UseQueryResult surface is irrelevant here.
  } as unknown as ReturnType<typeof useHostFilesystem>);
}

describe("splitTypedPath", () => {
  // input -> { dir (directory to list, "" = home), partial (prefix) }
  const cases: Array<{
    name: string;
    input: string;
    dir: string;
    partial: string;
  }> = [
    {
      name: "empty input lists home with no filter",
      input: "",
      dir: "",
      partial: "",
    },
    {
      name: "bare ~ lists home with no filter",
      input: "~",
      dir: "",
      partial: "",
    },
    {
      name: "bare fragment filters home by the fragment",
      input: "proj",
      dir: "",
      partial: "proj",
    },
    {
      name: "leading-slash fragment lists the root",
      input: "/foo",
      dir: "/",
      partial: "foo",
    },
    {
      name: "root alone lists the root with no filter",
      input: "/",
      dir: "/",
      partial: "",
    },
    {
      name: "nested path filters the parent by the trailing partial",
      input: "/Users/corey/pr",
      dir: "/Users/corey",
      partial: "pr",
    },
    {
      name: "trailing slash lists that directory with no filter",
      input: "/Users/corey/",
      dir: "/Users/corey",
      partial: "",
    },
    {
      name: "~/ fragment lists home by the partial",
      input: "~/proj",
      dir: "",
      partial: "proj",
    },
    {
      name: "surrounding whitespace is trimmed first",
      input: "  /a/b  ",
      dir: "/a",
      partial: "b",
    },
  ];

  it.each(cases)("$name", ({ input, dir: expectedDir, partial }) => {
    expect(splitTypedPath(input)).toEqual({ dir: expectedDir, partial });
  });
});

describe("WorkspacePathField", () => {
  // value ends in "/" so the partial is empty and every non-hidden
  // directory under it matches.
  const VALUE = "/Users/corey/";
  const ENTRIES: HostFilesystemEntry[] = [
    dir("projects", "/Users/corey/projects"),
    dir("downloads", "/Users/corey/downloads"),
    dir(".hidden", "/Users/corey/.hidden"),
    {
      name: "readme.txt",
      path: "/Users/corey/readme.txt",
      type: "file",
      bytes: 10,
      modified_at: 0,
    },
  ];

  beforeEach(() => {
    useHostFilesystemMock.mockReset();
    // jsdom has no scrollIntoView; the keyboard-highlight effect calls it.
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    cleanup();
  });

  it("opens the dropdown on focus and lists only non-hidden directories", () => {
    mockListing(ENTRIES);
    render(
      <WorkspacePathField
        hostId="host_1"
        value={VALUE}
        onChange={vi.fn()}
        onBrowse={vi.fn()}
        recent={[]}
      />,
    );
    fireEvent.focus(screen.getByTestId("workspace-path-input"));
    expect(screen.getByTestId("workspace-path-dropdown")).toBeTruthy();
    expect(screen.getByTestId("workspace-match-0").textContent).toContain("/Users/corey/projects");
    // Files and dot-dirs are filtered out — a workspace must be a
    // visible directory.
    expect(screen.queryByText("/Users/corey/readme.txt")).toBeNull();
    expect(screen.queryByText("/Users/corey/.hidden")).toBeNull();
  });

  it("calls onChange with the match path when a row is clicked", () => {
    // The regression: rows must be selectable. Selection fires on
    // mousedown (not click) so it beats the input's blur.
    mockListing(ENTRIES);
    const onChange = vi.fn();
    render(
      <WorkspacePathField
        hostId="host_1"
        value={VALUE}
        onChange={onChange}
        onBrowse={vi.fn()}
        recent={[]}
      />,
    );
    fireEvent.focus(screen.getByTestId("workspace-path-input"));
    fireEvent.mouseDown(screen.getByTestId("workspace-match-0"));
    expect(onChange).toHaveBeenCalledWith("/Users/corey/projects");
  });

  it("calls onChange when a recent row is clicked", () => {
    mockListing(ENTRIES);
    const onChange = vi.fn();
    render(
      <WorkspacePathField
        hostId="host_1"
        value={VALUE}
        onChange={onChange}
        onBrowse={vi.fn()}
        recent={["/Users/corey/recent-proj"]}
      />,
    );
    fireEvent.focus(screen.getByTestId("workspace-path-input"));
    fireEvent.mouseDown(screen.getByTestId("workspace-recent-0"));
    expect(onChange).toHaveBeenCalledWith("/Users/corey/recent-proj");
  });

  it("selects the first row via ArrowDown + Enter", () => {
    mockListing(ENTRIES);
    const onChange = vi.fn();
    render(
      <WorkspacePathField
        hostId="host_1"
        value={VALUE}
        onChange={onChange}
        onBrowse={vi.fn()}
        recent={[]}
      />,
    );
    const input = screen.getByTestId("workspace-path-input");
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("/Users/corey/projects");
  });

  it("commits the typed absolute path on Enter when no row is highlighted", () => {
    // The regression: typing a full path and pressing Enter (without
    // arrowing into the dropdown) did nothing. Enter on an absolute
    // path now fires onCommit (the dialog opens the tree browser at
    // it) and dismisses the autocomplete. onChange is not called — it
    // fires only for a row selection, not a typed commit.
    mockListing(ENTRIES);
    const onChange = vi.fn();
    const onCommit = vi.fn();
    render(
      <WorkspacePathField
        hostId="host_1"
        value={VALUE}
        onChange={onChange}
        onBrowse={vi.fn()}
        onCommit={onCommit}
        recent={[]}
      />,
    );
    const input = screen.getByTestId("workspace-path-input");
    fireEvent.focus(input);
    expect(screen.getByTestId("workspace-path-dropdown")).toBeTruthy();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.queryByTestId("workspace-path-dropdown")).toBeNull();
    expect(onCommit).toHaveBeenCalledWith("/Users/corey/");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("commits a tilde path on Enter (the host expands it)", () => {
    // Home-relative paths (~, ~/foo) are navigable — the host expands
    // them — so Enter must commit them just like absolute paths.
    mockListing(ENTRIES);
    const onCommit = vi.fn();
    render(
      <WorkspacePathField
        hostId="host_1"
        value="~/projects"
        onChange={vi.fn()}
        onBrowse={vi.fn()}
        onCommit={onCommit}
        recent={[]}
      />,
    );
    const input = screen.getByTestId("workspace-path-input");
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onCommit).toHaveBeenCalledWith("~/projects");
  });

  it("does not commit a non-absolute path on Enter", () => {
    // A bare fragment isn't an absolute host path, so Enter dismisses
    // the dropdown without opening the browser (which needs an
    // absolute path to navigate to).
    mockListing(ENTRIES);
    const onCommit = vi.fn();
    render(
      <WorkspacePathField
        hostId="host_1"
        value="proj"
        onChange={vi.fn()}
        onBrowse={vi.fn()}
        onCommit={onCommit}
        recent={[]}
      />,
    );
    const input = screen.getByTestId("workspace-path-input");
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("suppresses the dropdown while the tree browser is open", () => {
    // dropdownDisabled (the tree browser is showing) must hide the
    // autocomplete so the two pickers don't stack.
    mockListing(ENTRIES);
    render(
      <WorkspacePathField
        hostId="host_1"
        value={VALUE}
        onChange={vi.fn()}
        onBrowse={vi.fn()}
        recent={[]}
        dropdownDisabled
      />,
    );
    fireEvent.focus(screen.getByTestId("workspace-path-input"));
    expect(screen.queryByTestId("workspace-path-dropdown")).toBeNull();
  });
});
