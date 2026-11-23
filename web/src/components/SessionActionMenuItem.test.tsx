import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SessionActionMenuItem } from "./SessionActionMenuItem";

const HREF = "https://example.com/fork/app/blob/head/README.md";

function Menu({
  follow,
  onSelect,
  disabledReason,
}: {
  follow: (event: MouseEvent) => void;
  onSelect: () => void;
  disabledReason?: string;
}) {
  return (
    <TooltipProvider>
      <DropdownMenu>
        <DropdownMenuTrigger>More actions</DropdownMenuTrigger>
        <DropdownMenuContent>
          <SessionActionMenuItem
            link={{ href: HREF, follow }}
            disabledReason={disabledReason}
            onSelect={onSelect}
          >
            Open in GitHub
          </SessionActionMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </TooltipProvider>
  );
}

/** A follow that handles the click itself, as the in-app browser or a popup does. */
function handled() {
  return vi.fn((event: MouseEvent) => event.preventDefault());
}

async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "More actions" }));
  return screen.getByRole("menuitem", { name: "Open in GitHub" });
}

describe("SessionActionMenuItem as a link", () => {
  it("is a real link to the address, so hovering and copying it work", async () => {
    const user = userEvent.setup();
    render(<Menu follow={handled()} onSelect={vi.fn()} />);
    const row = await openMenu(user);
    expect(row.tagName).toBe("A");
    expect(row).toHaveAttribute("href", HREF);
    expect(row).toHaveAttribute("rel", "noopener noreferrer");
  });

  it.each([
    [
      "a click",
      async (user: ReturnType<typeof userEvent.setup>, row: HTMLElement) => user.click(row),
    ],
    [
      "Enter",
      async (user: ReturnType<typeof userEvent.setup>, row: HTMLElement) => {
        row.focus();
        await user.keyboard("{Enter}");
      },
    ],
    [
      "Space",
      async (user: ReturnType<typeof userEvent.setup>, row: HTMLElement) => {
        row.focus();
        await user.keyboard(" ");
      },
    ],
  ])("follows the link once on %s, and the menu still closes", async (_how, activate) => {
    const user = userEvent.setup();
    const follow = handled();
    const onSelect = vi.fn();
    render(<Menu follow={follow} onSelect={onSelect} />);
    const row = await openMenu(user);
    await activate(user, row);
    expect(follow).toHaveBeenCalledTimes(1);
    // The link is the action: the row's select doesn't open it a second time.
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("hands a modified click to the follow, which leaves it to the browser", async () => {
    const user = userEvent.setup();
    // Like the real one: a ⌘-click isn't handled, so the browser opens it in a new tab.
    const follow = vi.fn((event: MouseEvent) => {
      if (!event.metaKey) event.preventDefault();
    });
    render(<Menu follow={follow} onSelect={vi.fn()} />);
    const row = await openMenu(user);
    const clicks: MouseEvent[] = [];
    row.addEventListener("click", (event) => clicks.push(event));
    await user.keyboard("{Meta>}");
    await user.click(row);
    await user.keyboard("{/Meta}");
    expect(follow).toHaveBeenCalledTimes(1);
    expect(follow.mock.calls[0]![0].metaKey).toBe(true);
    expect(clicks.at(-1)?.defaultPrevented).toBe(false);
  });

  it("isn't a link while it can't run, and does nothing", async () => {
    const user = userEvent.setup();
    const follow = handled();
    const onSelect = vi.fn();
    render(
      <Menu
        follow={follow}
        onSelect={onSelect}
        disabledReason="The pull request's source repository isn't known."
      />,
    );
    const row = await openMenu(user);
    expect(row.tagName).not.toBe("A");
    expect(row).not.toHaveAttribute("href");
    expect(row).toHaveAttribute("aria-disabled", "true");
    expect(row).toHaveAccessibleDescription("The pull request's source repository isn't known.");
    await user.click(row);
    expect(follow).not.toHaveBeenCalled();
    expect(onSelect).not.toHaveBeenCalled();
  });
});
