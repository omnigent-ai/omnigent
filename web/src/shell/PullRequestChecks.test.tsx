import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { PullRequestCheckRun } from "@/newPullRequestPage/hooks/usePullRequests";
import { CheckRunsPopover } from "./PullRequestChecks";

const RUNS: PullRequestCheckRun[] = [
  { name: "CI / build", bucket: "passing", url: "https://ci.example.com/build" },
  { name: "Lint / gate / Security Gate", bucket: "failing", url: "https://ci.example.com/gate" },
  { name: "CI / docs", bucket: "pending", url: null },
];

const LABEL = "1 failing check · 1 of 3 passed";

/** The popover in a row that records what reaches it, as a list's row would act on it. */
function renderInRow(runs: readonly PullRequestCheckRun[] = RUNS) {
  const row = { click: vi.fn(), keyDown: vi.fn(), contextMenu: vi.fn(), pointerDown: vi.fn() };
  render(
    <div
      role="row"
      onClick={row.click}
      onKeyDown={row.keyDown}
      onContextMenu={row.contextMenu}
      onPointerDown={row.pointerDown}
    >
      <CheckRunsPopover
        label={LABEL}
        runs={runs}
        trigger={
          <button type="button" aria-label={LABEL}>
            ✖ 1
          </button>
        }
      />
    </div>,
  );
  return { row, trigger: screen.getByRole("button", { name: LABEL }) };
}

const jobs = () => screen.queryByRole("list", { name: "Checks" });

afterEach(cleanup);

describe("CheckRunsPopover", () => {
  it("opens the jobs on a click, failures first, without the row seeing it", async () => {
    const user = userEvent.setup();
    const { row, trigger } = renderInRow();
    await user.hover(trigger);
    // A plain hover only names the result.
    expect(jobs()).toBeNull();
    expect(trigger).toHaveAttribute("title", LABEL);

    await user.click(trigger);
    const list = within(jobs()!);
    expect(list.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      expect.stringMatching(/^Lint \/ gate \/ Security Gate/),
      expect.stringMatching(/^CI \/ docs/),
      expect.stringMatching(/^CI \/ build/),
    ]);
    expect(screen.getByText(LABEL, { selector: "p" })).toBeVisible();
    expect(trigger).not.toHaveAttribute("title");
    expect(row.click).not.toHaveBeenCalled();
    expect(row.pointerDown).not.toHaveBeenCalled();
  });

  it("opens from Enter and Space, and Escape closes it back onto the trigger", async () => {
    const user = userEvent.setup();
    const { row, trigger } = renderInRow();
    trigger.focus();
    await user.keyboard("{Enter}");
    expect(jobs()).not.toBeNull();
    await user.keyboard("{Escape}");
    expect(jobs()).toBeNull();
    expect(trigger).toHaveFocus();

    await user.keyboard(" ");
    expect(jobs()).not.toBeNull();
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    // Enter and Space were the trigger's, never the row's.
    const rowKeys = row.keyDown.mock.calls.map(([event]) => event.key);
    expect(rowKeys).not.toContain("Enter");
    expect(rowKeys).not.toContain(" ");
    expect(row.click).not.toHaveBeenCalled();
  });

  it("closes on a click outside", async () => {
    const user = userEvent.setup();
    const { trigger } = renderInRow();
    await user.click(trigger);
    expect(jobs()).not.toBeNull();
    await user.click(document.body);
    expect(jobs()).toBeNull();
  });

  it("keeps clicks on its jobs and their links off the row, and links still open", async () => {
    const user = userEvent.setup();
    const { row, trigger } = renderInRow();
    await user.click(trigger);
    const link = within(jobs()!).getByRole("link", {
      name: "Lint / gate / Security Gate: failing",
    });
    expect(link).toHaveAttribute("href", "https://ci.example.com/gate");
    expect(link).toHaveAttribute("target", "_blank");
    // jsdom can't open a window, so the test keeps the link from navigating.
    link.addEventListener("click", (event) => event.preventDefault());
    fireEvent.click(link);
    fireEvent.click(link, { shiftKey: true });
    fireEvent.contextMenu(within(jobs()!).getByText("CI / docs"));
    fireEvent.click(screen.getByText(LABEL, { selector: "p" }));
    expect(row.click).not.toHaveBeenCalled();
    expect(row.contextMenu).not.toHaveBeenCalled();
  });

  it("says so when the read named no jobs", async () => {
    const user = userEvent.setup();
    const { trigger } = renderInRow([]);
    await user.click(trigger);
    expect(screen.getByText("No job details.")).toBeVisible();
  });
});

describe("check rows", () => {
  it("end every job's state with the same link slot, whether or not the job links anywhere", async () => {
    const user = userEvent.setup();
    const { trigger } = renderInRow();
    await user.click(trigger);
    const rows = within(jobs()!).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    for (const item of rows) {
      const row = item.firstElementChild!;
      // The state is followed only by the slot, so the slot alone decides where it ends.
      const slot = row.lastElementChild!;
      expect(slot).toHaveAttribute("data-check-link-slot");
      expect(slot.previousElementSibling).toHaveTextContent(/^(successful|failing|running)$/);
    }
    const linked = rows.map((item) => item.querySelector("[data-check-link-slot] svg") !== null);
    expect(linked).toEqual([true, false, true]);
  });
});
