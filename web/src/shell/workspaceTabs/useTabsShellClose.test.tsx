import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TerminalInfo } from "@/lib/terminals";

import { useTabsShellClose } from "./useTabsShellClose";

const shell = (id: string, name: string): TerminalInfo => ({
  id,
  name,
  session: "s1",
  running: true,
});
const TERMINALS = [shell("t1", "zsh"), shell("t2", "bash"), shell("t3", "fish")];

function closer(deleteTerminal = vi.fn(async () => undefined)) {
  const view = renderHook(() => useTabsShellClose({ terminals: TERMINALS, deleteTerminal }));
  return { ...view, deleteTerminal };
}

afterEach(cleanup);

describe("useTabsShellClose", () => {
  it("asks about the shell, and closes its tab only on the dialog's yes", async () => {
    const { result } = closer();
    let closing!: Promise<boolean>;
    act(() => {
      closing = result.current.confirmCloseShell("terminal:t1");
    });
    expect(result.current.question?.label).toBe("zsh · s1");
    act(() => result.current.answerQuestion(true));
    await expect(closing).resolves.toBe(true);
    expect(result.current.question).toBeNull();

    act(() => {
      closing = result.current.confirmCloseShell("terminal:t2");
    });
    act(() => result.current.answerQuestion(false));
    await expect(closing).resolves.toBe(false);
  });

  it("keeps the older shell when a newer close takes over the question", async () => {
    const { result } = closer();
    let older!: Promise<boolean>;
    let newer!: Promise<boolean>;
    act(() => {
      older = result.current.confirmCloseShell("terminal:t1");
    });
    act(() => {
      newer = result.current.confirmCloseShell("terminal:t2");
    });
    await expect(older).resolves.toBe(false);
    expect(result.current.question?.label).toBe("bash · s1");
    act(() => result.current.answerQuestion(true));
    await expect(newer).resolves.toBe(true);
  });

  it("asks once for a split's shells, naming each, and skips one that's gone", async () => {
    const { result } = closer();
    let closing!: Promise<boolean>;
    act(() => {
      closing = result.current.confirmCloseShells([
        "terminal:t1",
        "terminal:gone",
        "terminal:t2",
        "terminal:t3",
      ]);
    });
    expect(result.current.question).toMatchObject({
      label: "zsh · s1, bash · s1 and fish · s1",
      count: 3,
    });
    act(() => result.current.answerQuestion(true));
    await expect(closing).resolves.toBe(true);

    // Two shells read "a and b"; a newer single close keeps them.
    act(() => {
      closing = result.current.confirmCloseShells(["terminal:t1", "terminal:t2"]);
    });
    expect(result.current.question?.label).toBe("zsh · s1 and bash · s1");
    act(() => void result.current.confirmCloseShell("terminal:t3"));
    await expect(closing).resolves.toBe(false);
    expect(result.current.question).toMatchObject({ label: "fish · s1", count: 1 });
  });

  it("lets a split whose shells are all gone close without asking", async () => {
    const { result } = closer();
    await expect(result.current.confirmCloseShells(["terminal:gone"])).resolves.toBe(true);
    expect(result.current.question).toBeNull();
  });

  it("ends the shell, says when it couldn't, and lets a gone shell's tab close", async () => {
    const { result, deleteTerminal } = closer();
    await expect(result.current.endShell("terminal:t2")).resolves.toBe(true);
    expect(deleteTerminal).toHaveBeenCalledWith("t2");
    await expect(result.current.confirmCloseShell("terminal:gone")).resolves.toBe(true);
    await expect(result.current.endShell("terminal:gone")).resolves.toBe(true);

    const failing = closer(vi.fn(async () => Promise.reject(new Error("offline"))));
    await expect(failing.result.current.endShell("terminal:t1")).resolves.toBe(false);
  });

  it("keeps the shell when the sidebar goes while it's still asking", async () => {
    const { result, unmount } = closer();
    let closing!: Promise<boolean>;
    act(() => {
      closing = result.current.confirmCloseShell("terminal:t1");
    });
    unmount();
    await expect(closing).resolves.toBe(false);
  });
});
