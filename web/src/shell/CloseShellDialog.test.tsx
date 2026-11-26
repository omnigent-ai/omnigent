import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CloseShellDialog } from "./CloseShellDialog";
import type { ShellBusy } from "./shellCloseCheck";

afterEach(cleanup);

function description(busy: ShellBusy | null) {
  render(
    <CloseShellDialog
      open
      shellLabel="zsh · s1"
      busy={busy}
      onConfirm={vi.fn()}
      onCancel={vi.fn()}
    />,
  );
  return screen.getByRole("dialog").querySelector("p")?.textContent;
}

describe("CloseShellDialog", () => {
  it.each([
    [null, "This terminates zsh · s1 and ends any process running in it. This can't be undone."],
    [
      { foreground: "vim", backgroundJobs: 0 },
      "vim is running in zsh · s1. Closing the shell ends everything running in it. This can't be undone.",
    ],
    [
      { foreground: "vim", backgroundJobs: 2 },
      "vim and 2 background jobs are running in zsh · s1. Closing the shell ends everything running in it. This can't be undone.",
    ],
    [
      { foreground: null, backgroundJobs: 1 },
      "1 background job is running in zsh · s1. Closing the shell ends everything running in it. This can't be undone.",
    ],
    // Busy, but nothing it could name: the plain copy.
    [
      { foreground: null, backgroundJobs: null },
      "This terminates zsh · s1 and ends any process running in it. This can't be undone.",
    ],
  ])("describes %j", (busy, text) => {
    expect(description(busy)).toBe(text);
  });

  it("shows a hostile command name as text, never as markup", () => {
    description({ foreground: "<img src=x>", backgroundJobs: 0 });
    expect(screen.getByRole("dialog").querySelector("img")).toBeNull();
    expect(screen.getByText("<img src=x>")).toBeTruthy();
  });
});
