import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { XIcon } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

import { PullRequestChipAction } from "./PullRequestSection";

describe("PullRequestChipAction", () => {
  it("runs its action on a press", async () => {
    const onClick = vi.fn();
    render(<PullRequestChipAction label="Remove bug" icon={XIcon} onClick={onClick} />);
    const button = screen.getByRole("button", { name: "Remove bug" });
    expect(button).not.toHaveAttribute("aria-disabled");
    await userEvent.click(button);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("stays reachable but runs nothing while disabled, and says why", async () => {
    const onClick = vi.fn();
    render(
      <PullRequestChipAction
        label="Remove bug"
        title="Only the session's owner can change labels"
        icon={XIcon}
        onClick={onClick}
        disabled
      />,
    );
    const button = screen.getByRole("button", { name: "Remove bug" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAttribute("title", "Only the session's owner can change labels");
    await userEvent.tab();
    expect(button).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });
});
