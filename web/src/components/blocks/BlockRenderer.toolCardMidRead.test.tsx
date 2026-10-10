// A tool card the user opened mid-turn must survive the run re-partitioning
// that happens when the next tool call lands: the live run folds everything
// before its visible tail, and an opened card must not be swept into that
// closed summary while the user is reading it.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { RenderItem } from "@/lib/renderItems";
import { TooltipProvider } from "@/components/ui/tooltip";
import { BlockRenderer } from "./BlockRenderer";

afterEach(cleanup);

const shell = (n: number): RenderItem => ({
  kind: "tool",
  itemId: `fc_${n}`,
  execution: {
    name: "sys_os_shell",
    arguments: { command: `echo probe-step-${n}` },
    argsSummary: "",
    callId: `c_${n}`,
    agentName: "nessie",
    executedBy: "server",
    output: "ok",
  },
  output: "ok",
  state: "output-available",
  startedAt: null,
  duration: undefined,
});

const message: RenderItem = { kind: "text", itemId: "m0", text: "Working.", final: true };

const liveRun = (steps: number) => (
  <TooltipProvider>
    <BlockRenderer
      items={[message, ...Array.from({ length: steps }, (_, i) => shell(i + 1))]}
      sessionStatus="running"
    />
  </TooltipProvider>
);

describe("BlockRenderer user-opened tool cards", () => {
  it("keeps a user-opened tool card out of the run fold when the next tool lands", () => {
    const { rerender } = render(liveRun(3));
    fireEvent.click(screen.getByRole("button", { name: /echo probe-step-1/ }));
    expect(screen.getByText("Parameters")).toBeDefined();

    rerender(liveRun(4));

    expect(screen.getByRole("button", { name: /echo probe-step-1/ })).toBeDefined();
    expect(screen.getByText("Parameters")).toBeDefined();
    expect(screen.queryByText("Ran 1 shell command")).toBeNull();
  });
});
