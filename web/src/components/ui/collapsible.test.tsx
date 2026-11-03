import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { Collapsible, CollapsibleTrigger, RetainedCollapsibleContent } from "./collapsible";

afterEach(cleanup);

function RetainedSection({ defaultOpen = false }: { defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger>Details</CollapsibleTrigger>
      <RetainedCollapsibleContent open={open}>
        <p>Rendered details</p>
      </RetainedCollapsibleContent>
    </Collapsible>
  );
}

function toggle() {
  fireEvent.click(screen.getByRole("button", { name: "Details" }));
}

function stubCloseAnimation(content: HTMLElement) {
  let finish!: () => void;
  const finished = new Promise<void>((resolve) => {
    finish = resolve;
  });
  content.getAnimations = () => [{ finished } as unknown as Animation];
  return async () => {
    await act(async () => {
      finish();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  };
}

describe("RetainedCollapsibleContent", () => {
  it("keeps the same children mounted and hides them while closed", () => {
    render(<RetainedSection />);
    const details = screen.getByText("Rendered details");
    expect(details).not.toBeVisible();

    toggle();
    expect(screen.getByText("Rendered details")).toBe(details);
    expect(details).toBeVisible();

    toggle();
    expect(screen.getByText("Rendered details")).toBe(details);
    expect(details).not.toBeVisible();
  });

  it("stays visible until its close animation finishes", async () => {
    render(<RetainedSection defaultOpen />);
    const details = screen.getByText("Rendered details");
    const finishAnimation = stubCloseAnimation(details.parentElement!);

    toggle();
    expect(details.parentElement).toHaveAttribute("data-state", "closed");
    expect(details).toBeVisible();

    await finishAnimation();
    expect(details).not.toBeVisible();
  });

  it("stays visible when reopened before its close animation finishes", async () => {
    render(<RetainedSection defaultOpen />);
    const details = screen.getByText("Rendered details");
    const finishAnimation = stubCloseAnimation(details.parentElement!);

    toggle();
    toggle();
    await finishAnimation();
    expect(details).toBeVisible();
  });
});
