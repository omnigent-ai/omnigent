import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ComposerAddMenu } from "./ComposerAddMenu";

const savedWorkflows = [
  {
    id: "a".repeat(32),
    name: "Triage flaky tests",
    description: "Finds and fixes flaky tests.\nGive it: the test suite.",
  },
  { id: "b".repeat(32), name: "Release notes", description: "Drafts the release notes." },
];

function renderMenu(props: Partial<Parameters<typeof ComposerAddMenu>[0]> = {}) {
  const handlers = {
    onAttach: vi.fn(),
    onWorkflow: vi.fn(),
    onRunSavedWorkflow: vi.fn(),
    onOpen: vi.fn(),
  };
  render(
    <ComposerAddMenu
      disabled={false}
      planActive={false}
      showGoal={false}
      showPlan={false}
      {...handlers}
      {...props}
    />,
  );
  fireEvent.pointerDown(screen.getByRole("button", { name: "Add" }), { button: 0 });
  return handlers;
}

const runAWorkflow = () => fireEvent.click(screen.getByTestId("composer-workflow-action"));

describe("ComposerAddMenu's Run a workflow", () => {
  afterEach(cleanup);

  it("opens the description dialog when there are no saved workflows", async () => {
    const { onWorkflow, onOpen } = renderMenu();
    expect(onOpen).toHaveBeenCalled();
    runAWorkflow();
    await waitFor(() => expect(onWorkflow).toHaveBeenCalledWith());
  });

  it("opens the description dialog when every saved workflow is a draft, which can't run", async () => {
    const { onWorkflow } = renderMenu({
      savedWorkflows: [
        { id: "c".repeat(32), name: "Unfinished workflow", description: "", is_draft: true },
      ],
    });
    runAWorkflow();
    await waitFor(() => expect(onWorkflow).toHaveBeenCalledWith());
    expect(screen.queryByTestId("composer-workflow-picker")).toBeNull();
  });

  it("names the owner of a workflow shared with the user, which runs like the user's own", async () => {
    const shared = {
      id: "d".repeat(32),
      name: "Weekly report",
      description: "Summarizes the week.",
      viewer_role: "viewer" as const,
      owner_display_name: "Alice",
    };
    const { onRunSavedWorkflow } = renderMenu({ savedWorkflows: [...savedWorkflows, shared] });
    runAWorkflow();
    const rows = screen.getAllByTestId("composer-saved-workflow");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Triage flaky testsFinds and fixes flaky tests.",
      "Release notesDrafts the release notes.",
      "Weekly reportShared by Alice · Summarizes the week.",
    ]);
    fireEvent.click(rows[2]!);
    await waitFor(() => expect(onRunSavedWorkflow).toHaveBeenCalledWith(shared));
  });

  it("opens the picker while more workflows can load, though none loaded yet show", async () => {
    const loadMore = vi.fn();
    const { onWorkflow } = renderMenu({
      savedWorkflows: [],
      workflowPagination: { hasMore: true, loadingMore: false, loadMore },
    });
    runAWorkflow();
    expect(await screen.findByTestId("composer-workflow-picker")).toBeInTheDocument();
    expect(onWorkflow).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Load more workflows"));
    expect(loadMore).toHaveBeenCalledOnce();
  });

  it("lists the saved workflows to search and run", async () => {
    const loadMore = vi.fn();
    const { onRunSavedWorkflow, onWorkflow } = renderMenu({
      workflowPagination: { hasMore: true, loadingMore: false, loadMore },
      savedWorkflows: [
        ...savedWorkflows,
        {
          id: "c".repeat(32),
          name: "Unfinished workflow",
          description: "Still planning.",
          is_draft: true,
        },
      ],
    });
    runAWorkflow();
    const names = () =>
      screen.getAllByTestId("composer-saved-workflow").map((row) => row.textContent);
    expect(names()).toEqual([
      "Triage flaky testsFinds and fixes flaky tests.",
      "Release notesDrafts the release notes.",
    ]);
    fireEvent.click(screen.getByText("Load more workflows"));
    expect(loadMore).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("Search workflows")).toBeVisible();
    expect(onRunSavedWorkflow).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Search workflows"), { target: { value: "TRIAGE" } });
    expect(names()).toEqual(["Triage flaky testsFinds and fixes flaky tests."]);
    fireEvent.click(screen.getByTestId("composer-saved-workflow"));

    await waitFor(() => expect(onRunSavedWorkflow).toHaveBeenCalledWith(savedWorkflows[0]));
    expect(onWorkflow).not.toHaveBeenCalled();
  });

  it("describes a new workflow, starting from what was typed", async () => {
    const { onWorkflow } = renderMenu({ savedWorkflows });
    runAWorkflow();
    fireEvent.change(screen.getByLabelText("Search workflows"), { target: { value: "Bump deps" } });
    expect(screen.getByText("No matching workflows")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("composer-describe-workflow"));
    await waitFor(() => expect(onWorkflow).toHaveBeenCalledWith("Bump deps"));
  });

  it("moves between the search and the list with the arrow keys", async () => {
    const { onRunSavedWorkflow } = renderMenu({ savedWorkflows });
    runAWorkflow();
    const search = screen.getByLabelText("Search workflows");
    fireEvent.change(search, { target: { value: "release" } });
    fireEvent.keyDown(search, { key: "ArrowDown" });
    const row = screen.getByTestId("composer-saved-workflow");
    expect(row).toHaveFocus();
    fireEvent.keyDown(row, { key: "ArrowUp" });
    expect(search).toHaveFocus();

    fireEvent.keyDown(search, { key: "ArrowDown" });
    fireEvent.keyDown(row, { key: "Enter" });
    await waitFor(() => expect(onRunSavedWorkflow).toHaveBeenCalledWith(savedWorkflows[1]));
  });
});

describe("ComposerAddMenu's placement", () => {
  afterEach(cleanup);

  function rect(left: number, top: number, width: number, height: number): DOMRect {
    return {
      left,
      top,
      width,
      height,
      right: left + width,
      bottom: top + height,
      x: left,
      y: top,
      toJSON: () => ({}),
    } as DOMRect;
  }

  /** Opens the menu inside a composer card measured at 640px wide, with the trigger near its foot. */
  function openInCard(appearance?: "panel") {
    render(
      <div data-composer-card="" data-composer-appearance={appearance} data-testid="card">
        <ComposerAddMenu
          disabled={false}
          planActive={false}
          showGoal={false}
          showPlan={false}
          onAttach={vi.fn()}
        />
      </div>,
    );
    vi.spyOn(screen.getByTestId("card"), "getBoundingClientRect").mockReturnValue(
      rect(20, 40, 640, 400),
    );
    const trigger = screen.getByRole("button", { name: "Add" });
    vi.spyOn(trigger, "getBoundingClientRect").mockReturnValue(rect(32, 400, 28, 28));
    fireEvent.pointerDown(trigger, { button: 0 });
    return screen.getByTestId("composer-add-menu");
  }

  /** Where the popper placed the menu, once it has measured. */
  async function placement(menu: HTMLElement): Promise<string> {
    const wrapper = menu.closest<HTMLElement>("[data-radix-popper-content-wrapper]")!;
    await waitFor(() => expect(wrapper.style.transform).toMatch(/translate/));
    return wrapper.style.transform;
  }

  it("spans the composer card, just above it, in a main window", async () => {
    const menu = openInCard();
    expect(menu).toHaveStyle({ width: "640px" });
    // Its bottom edge sits on the card's top (40), not on the trigger (400).
    expect(await placement(menu)).toMatch(/, 34px\)$/);
  });

  // In a panel the card is the whole window, so card geometry would pin the menu to its top.
  it("opens from the trigger at its own width in a panel", async () => {
    const menu = openInCard("panel");
    expect(menu.style.width).toBe("");
    expect(menu).toHaveAttribute("data-side", "top");
    expect(menu).toHaveAttribute("data-align", "start");
    // Starts at the trigger's left (32) and ends 6px above its top (400).
    expect(await placement(menu)).toBe("translate(32px, 394px)");
  });
});
