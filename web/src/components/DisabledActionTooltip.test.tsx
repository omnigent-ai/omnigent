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
