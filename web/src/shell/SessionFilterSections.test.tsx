import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  DEFAULT_SESSION_LIST_FILTER,
  SESSION_STATUS_FILTER_LABELS,
  SESSION_STATUS_FILTERS,
  type SessionListFilter,
} from "@/lib/sessionListFilters";

import { SessionFilterSections, sessionListFilterSummary } from "./SessionFilterSections";
import { STATUS_BUCKETS } from "./sidebarNav";

const HARNESSES = [
  { key: "native:claude", name: "Claude Code" },
  { key: "codex", name: "Codex SDK" },
];
const PROJECTS = [
  { id: "p-alpha", name: "Alpha" },
  { id: "p-beta", name: "Beta" },
];

function renderSections(value: Partial<SessionListFilter> = {}) {
  const onChange = vi.fn();
  const onClear = vi.fn();
  render(
    <DropdownMenu open>
      <DropdownMenuTrigger>Filter</DropdownMenuTrigger>
      <DropdownMenuContent>
        <SessionFilterSections
          value={{ ...DEFAULT_SESSION_LIST_FILTER, ...value }}
          onChange={onChange}
          onClear={onClear}
          harnesses={HARNESSES}
          projects={PROJECTS}
        />
      </DropdownMenuContent>
    </DropdownMenu>,
  );
  return { onChange, onClear };
}

const checked = () =>
  screen
    .getAllByRole("menuitemradio")
    .filter((item) => item.getAttribute("aria-checked") === "true")
    .map((item) => item.textContent);

describe("SessionFilterSections", () => {
  it("names each status as the Status grouping names its group", () => {
    expect(
      SESSION_STATUS_FILTERS.slice(1).map((status) => SESSION_STATUS_FILTER_LABELS[status]),
    ).toEqual([...STATUS_BUCKETS]);
  });

  it("lists a section for each filter, with All checked in each and no Clear filters", () => {
    renderSections();
    expect(screen.getByText("Pull request")).toBeInTheDocument();
    expect(screen.getByText("Status")).toBeInTheDocument();
    expect(screen.getByText("Harness")).toBeInTheDocument();
    expect(screen.getByText("Project")).toBeInTheDocument();
    expect(checked()).toEqual(["All sessions", "Any status", "Any harness", "Any project"]);
    expect(screen.getAllByTestId("session-harness-filter").map((item) => item.textContent)).toEqual(
      ["Claude Code", "Codex SDK"],
    );
    expect(screen.getAllByTestId("session-project-filter").map((item) => item.textContent)).toEqual(
      ["Alpha", "Beta"],
    );
    expect(screen.getByTestId("session-project-filter-none")).toHaveTextContent("No project");
    expect(screen.queryByText("Clear filters")).toBeNull();
    expect(sessionListFilterSummary(DEFAULT_SESSION_LIST_FILTER)).toBeNull();
  });

  it("changes only the filter chosen", () => {
    const { onChange } = renderSections();
    fireEvent.click(screen.getByText("Working"));
    expect(onChange).toHaveBeenLastCalledWith({ status: "working" });
    fireEvent.click(screen.getByText("Codex SDK"));
    expect(onChange).toHaveBeenLastCalledWith({ harness: "codex" });
    fireEvent.click(screen.getByText("Beta"));
    expect(onChange).toHaveBeenLastCalledWith({ project: { kind: "project", id: "p-beta" } });
    fireEvent.click(screen.getByText("No project"));
    expect(onChange).toHaveBeenLastCalledWith({ project: { kind: "none" } });
    fireEvent.click(screen.getByText("With an open pull request"));
    expect(onChange).toHaveBeenLastCalledWith({ pullRequest: "open" });
  });

  it("offers sessions without a harness as a choice of their own", () => {
    const harnesses = [...HARNESSES, { key: "", name: "Unknown harness" }];
    const onChange = vi.fn();
    render(
      <DropdownMenu open>
        <DropdownMenuTrigger>Filter</DropdownMenuTrigger>
        <DropdownMenuContent>
          <SessionFilterSections
            value={{ ...DEFAULT_SESSION_LIST_FILTER, harness: "" }}
            onChange={onChange}
            onClear={vi.fn()}
            harnesses={harnesses}
            projects={PROJECTS}
          />
        </DropdownMenuContent>
      </DropdownMenu>,
    );
    expect(checked()).toContain("Unknown harness");
    fireEvent.click(screen.getByText("Codex SDK"));
    expect(onChange).toHaveBeenLastCalledWith({ harness: "codex" });
    fireEvent.click(screen.getByText("Unknown harness"));
    expect(onChange).toHaveBeenLastCalledWith({ harness: "" });
  });

  it("goes back to All in a section", () => {
    const { onChange } = renderSections({ harness: "codex", project: { kind: "none" } });
    fireEvent.click(screen.getByText("Any harness"));
    expect(onChange).toHaveBeenLastCalledWith({ harness: null });
    fireEvent.click(screen.getByText("Any project"));
    expect(onChange).toHaveBeenLastCalledWith({ project: { kind: "all" } });
  });

  it("offers Clear filters and says how many are on while any is", () => {
    const value = { status: "done" as const, project: { kind: "project" as const, id: "p-alpha" } };
    const { onClear } = renderSections(value);
    expect(checked()).toEqual(["All sessions", "Done", "Any harness", "Alpha"]);
    expect(sessionListFilterSummary({ ...DEFAULT_SESSION_LIST_FILTER, ...value })).toBe("2 on");
    fireEvent.click(screen.getByText("Clear filters"));
    expect(onClear).toHaveBeenCalledOnce();
  });

  it("shows a chosen project that's gone as Missing project, checked, until cleared", () => {
    renderSections({ project: { kind: "project", id: "p-deleted" } });
    expect(screen.getByTestId("session-project-filter-missing")).toHaveTextContent(
      "Missing project",
    );
    expect(checked()).toContain("Missing project");
  });
});
