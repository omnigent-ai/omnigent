import { describe, expect, it, vi } from "vitest";
import { runCommand } from "./designCommands";

describe("design commands", () => {
  it("doesn't run a blocked command, and says why", () => {
    const run = vi.fn();
    const command = { blocked: "Draw on the page first.", run };
    expect(runCommand(command)).toEqual({ ok: false, error: "Draw on the page first." });
    expect(run).not.toHaveBeenCalled();
    expect(runCommand({ run })).toEqual({ ok: true });
    expect(run).toHaveBeenCalledTimes(1);
  });
});
