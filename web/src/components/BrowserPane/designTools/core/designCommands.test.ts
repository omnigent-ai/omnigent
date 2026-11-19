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

  it("logs a command that fails, thrown or rejected, and still answers", async () => {
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const thrown = () => {
      throw new Error("undo broke");
    };
    expect(runCommand({ run: thrown })).toEqual({ ok: true });
    expect(runCommand({ run: () => Promise.reject(new Error("send broke")) })).toEqual({
      ok: true,
    });
    await vi.waitFor(() => expect(logged).toHaveBeenCalledTimes(2));
    logged.mockRestore();
  });
});
