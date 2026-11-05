import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RewindPicker } from "@/components/RewindPicker";
import * as sessionsApi from "@/lib/sessionsApi";

function renderPicker(open = true) {
  const onPick = vi.fn();
  const onOpenChange = vi.fn();
  const view = render(
    <RewindPicker sessionId="conv_abc" open={open} onOpenChange={onOpenChange} onPick={onPick} />,
  );
  const reopen = (next: boolean) =>
    view.rerender(
      <RewindPicker sessionId="conv_abc" open={next} onOpenChange={onOpenChange} onPick={onPick} />,
    );
  return { onPick, onOpenChange, reopen };
}

const rowTexts = (picker: HTMLElement) =>
  within(picker)
    .queryAllByTestId("rewind-prompt")
    .map((row) => row.textContent);

describe("RewindPicker", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("loads older prompts on request, whatever the search", async () => {
    const prompts = vi
      .spyOn(sessionsApi, "fetchRewindPrompts")
      .mockResolvedValueOnce({
        prompts: [
          { itemId: "msg_recent", text: "Add parser tests" },
          { itemId: "msg_attachment", text: "" },
        ],
        olderCursor: "msg_cursor",
      })
      .mockRejectedValueOnce(new Error("Network down"))
      .mockResolvedValueOnce({
        prompts: [{ itemId: "msg_oldest", text: "Set up the repo" }],
        olderCursor: null,
      });
    const { onPick } = renderPicker();
    const picker = await screen.findByTestId("rewind-picker");
    await within(picker).findAllByTestId("rewind-prompt");
    const search = within(picker).getByRole("combobox");

    fireEvent.change(search, { target: { value: "repo" } });
    expect(rowTexts(picker)).toEqual([]);
    fireEvent.click(within(picker).getByTestId("rewind-load-older"));

    // A failed older page keeps the search, the loaded rows and the cursor.
    const loadOlder = await within(picker).findByText("Network down — try again");
    expect(search).toHaveValue("repo");
    fireEvent.change(search, { target: { value: "" } });
    expect(rowTexts(picker)).toEqual(["Add parser tests", "(attachment)"]);
    fireEvent.change(search, { target: { value: "repo" } });
    fireEvent.click(loadOlder);

    const older = await within(picker).findByText("Set up the repo");
    expect(prompts.mock.calls).toEqual([
      ["conv_abc", expect.any(AbortSignal)],
      ["conv_abc", expect.any(AbortSignal), "msg_cursor"],
      ["conv_abc", expect.any(AbortSignal), "msg_cursor"],
    ]);
    expect(within(picker).queryByTestId("rewind-load-older")).toBeNull();
    fireEvent.click(older);
    // An attachment-only prompt is listed by its label and picked by its id.
    fireEvent.change(search, { target: { value: "" } });
    fireEvent.click(within(picker).getByText("(attachment)"));
    expect(onPick.mock.calls).toEqual([
      [{ itemId: "msg_oldest", text: "Set up the repo" }],
      [{ itemId: "msg_attachment", text: "" }],
    ]);
  });

  it("moves, loads older prompts and picks one from the keyboard alone", async () => {
    vi.spyOn(sessionsApi, "fetchRewindPrompts")
      .mockResolvedValueOnce({
        prompts: [
          { itemId: "msg_recent", text: "Add parser tests" },
          { itemId: "msg_middle", text: "Fix the lexer" },
        ],
        olderCursor: "msg_cursor",
      })
      .mockResolvedValueOnce({
        prompts: [{ itemId: "msg_oldest", text: "Set up the repo" }],
        olderCursor: null,
      });
    const { onPick, onOpenChange } = renderPicker();
    const picker = await screen.findByTestId("rewind-picker");
    await within(picker).findAllByTestId("rewind-prompt");
    const search = within(picker).getByRole("combobox");
    expect(search).toHaveFocus();
    const highlighted = () => within(picker).getByRole("option", { selected: true }).textContent;

    expect(highlighted()).toBe("Add parser tests");
    fireEvent.keyDown(search, { key: "ArrowDown" });
    fireEvent.keyDown(search, { key: "ArrowDown" });
    expect(highlighted()).toBe("Load older prompts");
    fireEvent.keyDown(search, { key: "Enter" });
    await within(picker).findByText("Set up the repo");
    expect(onPick).not.toHaveBeenCalled();

    // The list loops, so ArrowUp from the newest prompt reaches the oldest.
    fireEvent.keyDown(search, { key: "Home" });
    fireEvent.keyDown(search, { key: "ArrowUp" });
    expect(highlighted()).toBe("Set up the repo");
    fireEvent.keyDown(search, { key: "Enter" });
    expect(onPick).toHaveBeenCalledExactlyOnceWith({
      itemId: "msg_oldest",
      text: "Set up the repo",
    });

    fireEvent.keyDown(search, { key: "Escape" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("ignores prompts that arrive after it was closed", async () => {
    let answerFirstOpen!: (page: sessionsApi.RewindPromptsPage) => void;
    vi.spyOn(sessionsApi, "fetchRewindPrompts")
      .mockReturnValueOnce(
        new Promise((resolve) => {
          answerFirstOpen = resolve;
        }),
      )
      .mockResolvedValueOnce({
        prompts: [{ itemId: "msg_current", text: "Current prompt" }],
        olderCursor: null,
      });
    const { reopen } = renderPicker();
    await screen.findByTestId("rewind-picker");

    reopen(false);
    reopen(true);
    const picker = await screen.findByTestId("rewind-picker");
    await within(picker).findByText("Current prompt");
    await act(async () =>
      answerFirstOpen({
        prompts: [{ itemId: "msg_stale", text: "Stale prompt" }],
        olderCursor: "msg_stale",
      }),
    );

    expect(rowTexts(picker)).toEqual(["Current prompt"]);
    expect(within(picker).queryByTestId("rewind-load-older")).toBeNull();
  });

  it("shows a prompt list that failed to load", async () => {
    vi.spyOn(sessionsApi, "fetchRewindPrompts").mockRejectedValue(new Error("Session not found"));
    renderPicker();

    expect(await screen.findByRole("alert")).toHaveTextContent("Session not found");
  });
});
