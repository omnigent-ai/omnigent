import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { DisabledActionTooltip } from "./DisabledActionTooltip";

afterEach(cleanup);

it("moves focus to the enabled button when its restriction clears", async () => {
  const user = userEvent.setup();
  const onClick = vi.fn();
  const { rerender } = render(
    <DisabledActionTooltip reason="Checking session capabilities…" label="Fork">
      <button type="button" disabled onClick={onClick}>
        Fork
      </button>
    </DisabledActionTooltip>,
  );
  await user.tab();
  expect(screen.getByRole("group", { name: "Fork" })).toHaveFocus();
  await waitFor(() =>
    expect(screen.getByRole("tooltip")).toHaveTextContent("Checking session capabilities…"),
  );

  rerender(
    <DisabledActionTooltip label="Fork">
      <button type="button" onClick={onClick}>
        Fork
      </button>
    </DisabledActionTooltip>,
  );
  expect(screen.getByRole("button", { name: "Fork" })).toHaveFocus();
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  await user.keyboard("{Enter}");
  expect(onClick).toHaveBeenCalledOnce();
});

it("does not reopen a cleared tooltip when the restriction returns", async () => {
  const user = userEvent.setup();
  const content = (reason?: string) => (
    <>
      <DisabledActionTooltip reason={reason}>
        <button type="button">Fork</button>
      </DisabledActionTooltip>
      <button type="button">Another action</button>
    </>
  );
  const { rerender } = render(content("Checking session capabilities…"));
  await user.tab();
  await waitFor(() => expect(screen.getByRole("tooltip")).toBeVisible());

  rerender(content());
  await user.tab();
  expect(screen.getByRole("button", { name: "Another action" })).toHaveFocus();
  rerender(content("Checking session capabilities…"));
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
});
