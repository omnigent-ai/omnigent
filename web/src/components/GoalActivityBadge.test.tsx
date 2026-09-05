import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { GoalActivityBadge } from "./GoalActivityBadge";

afterEach(cleanup);
it.each(["active", "paused"] as const)(
  "labels a %s Goal independently of execution status",
  (state) => {
    render(
      <TooltipProvider>
        <GoalActivityBadge state={state} />
      </TooltipProvider>,
    );
    expect(screen.getByRole("img", { name: `Goal ${state}` })).toHaveTextContent("G");
  },
);
