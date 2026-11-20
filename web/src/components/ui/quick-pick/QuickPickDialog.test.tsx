import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { createPortal } from "react-dom";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";

import type { QuickPickAcceptResult, QuickPickItem } from "./model";
import type { QuickPickSegment } from "./presentation";
import { QuickPickDialog, type QuickPickDialogAcceptResult } from "./QuickPickDialog";
import { useQuickPickPages } from "./useQuickPickPages";

afterEach(cleanup);

function item(id: string, overrides: Partial<QuickPickItem<string>> = {}): QuickPickItem<string> {
  return { id, label: id, value: id, ...overrides };
}

const segment = (items: QuickPickItem<string>[]): QuickPickSegment<string>[] => [
  { sourceId: "commands", ranking: "client", items },
];

const commands = segment([
  item("new", { label: "New session" }),
  item("settings", { label: "Open settings" }),
]);

function DialogHarness({
  onAccept = () => undefined,
  onCancel,
}: {
  onAccept?: (accepted: QuickPickItem<string>) => QuickPickAcceptResult;
  onCancel?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open picker
      </button>
      <QuickPickDialog
        open={open}
        onOpenChange={setOpen}
        onCancel={onCancel}
        label="Run a command"
        segments={commands}
        query={query}
        onQueryChange={setQuery}
        emptyMessage="No results found"
        onAccept={onAccept}
      />
    </>
  );
}

async function openPicker() {
  const trigger = screen.getByRole("button", { name: "Open picker" });
  await userEvent.click(trigger);
  const input = await screen.findByRole("combobox", { name: "Run a command" });
  await waitFor(() => expect(input).toHaveFocus());
  return { trigger, input };
}

describe("QuickPickDialog", () => {
  it("focuses the query, closes on accept, and returns focus to the opener", async () => {
    render(<DialogHarness />);
    const { trigger } = await openPicker();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("runs an afterClose action once closed, in place of returning focus", async () => {
    const elsewhere = document.createElement("input");
    document.body.append(elsewhere);
    onTestFinished(() => elsewhere.remove());
    const afterClose = vi.fn(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
      elsewhere.focus();
    });
    render(<DialogHarness onAccept={() => ({ afterClose })} />);
    await openPicker();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(afterClose).toHaveBeenCalledOnce());
    expect(elsewhere).toHaveFocus();
  });

  it("stays open when onAccept keeps it open, and cancels on Escape", async () => {
    const onCancel = vi.fn();
    render(<DialogHarness onAccept={() => "keepOpen"} onCancel={onCancel} />);
    await openPicker();
    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(onCancel).not.toHaveBeenCalled();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("closes from the footer's Close hint", async () => {
    render(<DialogHarness />);
    await openPicker();
    await userEvent.click(screen.getByRole("button", { name: "Close (Escape)" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});

describe("QuickPickDialog over other layers", () => {
  it("returns focus to an opener inside the top of two open dialogs", async () => {
    // Stand-ins for two portaled dialogs already open, the opener in the upper one.
    const lower = document.createElement("div");
    lower.setAttribute("role", "dialog");
    lower.dataset.state = "open";
    const upper = lower.cloneNode() as HTMLElement;
    document.body.append(lower, upper);
    onTestFinished(() => {
      lower.remove();
      upper.remove();
    });
    function Layered() {
      const [open, setOpen] = useState(false);
      return (
        <>
          {createPortal(
            <button type="button" onClick={() => setOpen(true)}>
              Open picker
            </button>,
            upper,
          )}
          <QuickPickDialog
            open={open}
            onOpenChange={setOpen}
            label="Run a command"
            segments={commands}
            query=""
            onQueryChange={() => undefined}
            emptyMessage="No results found"
            onAccept={() => undefined}
          />
        </>
      );
    }
    render(<Layered />);
    const { trigger } = await openPicker();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByRole("combobox")).toBeNull());
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});

describe("QuickPickDialog child pages", () => {
  function PagedHarness({
    onCancel = () => undefined,
    acceptChild = () => undefined,
  }: {
    onCancel?: () => void;
    acceptChild?: () => QuickPickDialogAcceptResult;
  }) {
    const [open, setOpen] = useState(true);
    const pages = useQuickPickPages<"root" | "child">("root");
    return (
      <QuickPickDialog
        open={open}
        onOpenChange={setOpen}
        onCancel={onCancel}
        label={pages.page.id}
        heading={pages.page.id === "child" ? { label: "Appearance" } : undefined}
        segments={pages.page.id === "root" ? commands : segment([item("child-item")])}
        emptyMessage="No results found"
        {...pages.pickerProps}
        onAccept={(accepted) => {
          if (accepted.id === "new") {
            pages.push("child");
            return "keepOpen";
          }
          return accepted.id === "child-item" ? acceptChild() : undefined;
        }}
      />
    );
  }

  it.each([
    ["Escape", () => userEvent.keyboard("{Escape}")],
    ["Backspace", () => userEvent.keyboard("{Backspace}")],
    ["the Back button", () => userEvent.click(screen.getByRole("button", { name: "Back" }))],
    [
      "the footer's Back hint",
      () => userEvent.click(screen.getByRole("button", { name: "Back (Escape)" })),
    ],
  ])("drops a child row's pending result when the user goes back with %s", async (_, goBack) => {
    let resolve: (result: QuickPickAcceptResult) => void = () => undefined;
    const abandoned = vi.fn();
    render(
      <PagedHarness
        acceptChild={() =>
          new Promise<QuickPickAcceptResult>((resolvePromise) => {
            resolve = resolvePromise;
          })
        }
      />,
    );
    await userEvent.keyboard("{Enter}");
    await userEvent.keyboard("{Enter}");
    await goBack();
    expect(screen.getByRole("combobox", { name: "root" })).toBeInTheDocument();
    await act(async () => {
      resolve({ afterClose: abandoned });
      await Promise.resolve();
    });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(abandoned).not.toHaveBeenCalled();
  });

  it("goes back on Escape in a child page, and cancels on Escape at the root", async () => {
    const onCancel = vi.fn();
    render(<PagedHarness onCancel={onCancel} />);
    const rootInput = screen.getByRole("combobox", { name: "root" });
    await userEvent.keyboard("{Enter}");
    // The same picker shows the child page, its query still focused, without remounting.
    expect(screen.getByRole("combobox", { name: "child" })).toBe(rootInput);
    expect(rootInput).toHaveFocus();
    expect(document.querySelector("[data-slot=quick-pick-heading]")).toHaveTextContent(
      "Appearance",
    );
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("combobox", { name: "root" })).toHaveFocus();
    expect(onCancel).not.toHaveBeenCalled();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("does not cancel when an accept closes the dialog", async () => {
    const onCancel = vi.fn();
    render(<PagedHarness onCancel={onCancel} />);
    await userEvent.keyboard("{ArrowDown}{Enter}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onCancel).not.toHaveBeenCalled();
  });
});

describe("QuickPickDialog reopened while closing", () => {
  it("drops the accepted row's action when reopened during its exit, and keeps the picker focused", async () => {
    const elsewhere = document.createElement("input");
    document.body.append(elsewhere);
    onTestFinished(() => elsewhere.remove());
    const afterClose = vi.fn(() => {
      // An action that moves focus, run only once the dialog is gone.
      expect(document.querySelector("[data-quick-pick-dialog]")).toBeNull();
      elsewhere.focus();
    });
    let setOpen: (open: boolean) => void = () => undefined;
    function Reopening() {
      const [open, setOpenState] = useState(true);
      setOpen = setOpenState;
      return (
        <QuickPickDialog
          open={open}
          onOpenChange={setOpenState}
          label="Pick"
          segments={commands}
          query=""
          onQueryChange={() => undefined}
          emptyMessage="No results found"
          onAccept={() => ({ afterClose })}
        />
      );
    }
    // The content's animation changes from its enter to its exit animation as it closes, so
    // Radix keeps it mounted until the exit ends, as in the app.
    let animationName = "enter";
    const computedStyle = window.getComputedStyle.bind(window);
    const styleSpy = vi.spyOn(window, "getComputedStyle").mockImplementation((element) => {
      const style = computedStyle(element);
      if (!(element instanceof HTMLElement) || !element.hasAttribute("data-quick-pick-dialog")) {
        return style;
      }
      return new Proxy(style, {
        get: (target, property) =>
          property === "animationName" ? animationName : Reflect.get(target, property),
      });
    });
    // Restored midway, once the exit animation no longer matters, and on any failure before it.
    onTestFinished(() => styleSpy.mockRestore());
    render(<Reopening />);
    const input = await screen.findByRole("combobox", { name: "Pick" });
    const content = screen.getByRole("dialog");
    animationName = "exit";
    await userEvent.keyboard("{Enter}");
    // Still mounted, closing: the role query skips it once it's hidden from assistive tech.
    expect(document.querySelector("[data-quick-pick-dialog]")).toBe(content);
    expect(content.dataset.state).toBe("closed");
    act(() => setOpen(true));
    await waitFor(() => expect(input).toHaveFocus());
    expect(afterClose).not.toHaveBeenCalled();

    // Neither a later cancel nor the next accept's close runs the dropped action.
    animationName = "none";
    styleSpy.mockRestore();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(afterClose).not.toHaveBeenCalled();

    // An accept that closes uninterrupted runs its action once closed, and the focus it moved stays.
    act(() => setOpen(true));
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(afterClose).toHaveBeenCalledTimes(1));
    expect(elsewhere).toHaveFocus();
  });

  it("drops an accepted row's action when the dialog stays open and is then cancelled", async () => {
    const afterClose = vi.fn();
    function KeptOpen() {
      const [open, setOpen] = useState(true);
      const [cancelled, setCancelled] = useState(false);
      return (
        <QuickPickDialog
          open={open && !cancelled}
          // The parent ignores the accept's close, and closes only on the cancel.
          onOpenChange={() => setOpen(true)}
          onCancel={() => setCancelled(true)}
          label="Pick"
          segments={commands}
          query=""
          onQueryChange={() => undefined}
          emptyMessage="No results found"
          onAccept={() => ({ afterClose })}
        />
      );
    }
    render(<KeptOpen />);
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(afterClose).not.toHaveBeenCalled();
  });
});

describe("QuickPickDialog unmounted by its owner", () => {
  function Conditional({ accept }: { accept: () => QuickPickDialogAcceptResult }) {
    const [open, setOpen] = useState(true);
    // The owner renders the dialog only while it's open, so an accepted close unmounts it.
    return open ? (
      <QuickPickDialog
        open
        onOpenChange={setOpen}
        label="Pick"
        segments={commands}
        query=""
        onQueryChange={() => undefined}
        emptyMessage="No results found"
        onAccept={accept}
      />
    ) : null;
  }

  it("still runs an accepted row's action once the close has unmounted it", async () => {
    const afterClose = vi.fn();
    render(<Conditional accept={() => ({ afterClose })} />);
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(afterClose).toHaveBeenCalledOnce());
    expect(document.querySelector("[data-quick-pick-dialog]")).toBeNull();
  });

  it("runs a deferred row's action once its accepted close has unmounted it", async () => {
    const afterClose = vi.fn();
    let resolve: (result: QuickPickAcceptResult) => void = () => undefined;
    render(
      <Conditional
        accept={() =>
          new Promise<QuickPickAcceptResult>((resolvePromise) => {
            resolve = resolvePromise;
          })
        }
      />,
    );
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    await act(async () => {
      resolve({ afterClose });
      await Promise.resolve();
    });
    await waitFor(() => expect(afterClose).toHaveBeenCalledOnce());
  });
});

describe("QuickPickDialog kept open by its owner", () => {
  it("drops an accepted row's action when its owner kept it open, then closed it from outside", async () => {
    const afterClose = vi.fn();
    let closeFromOutside: () => void = () => undefined;
    function KeptOpen() {
      const [open, setOpen] = useState(true);
      closeFromOutside = () => setOpen(false);
      return (
        <QuickPickDialog
          open={open}
          // The owner ignores the accept's close.
          onOpenChange={() => setOpen(true)}
          label="Pick"
          segments={commands}
          query=""
          onQueryChange={() => undefined}
          emptyMessage="No results found"
          onAccept={() => ({ afterClose })}
        />
      );
    }
    render(<KeptOpen />);
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    act(() => closeFromOutside());
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(afterClose).not.toHaveBeenCalled();
  });
});

describe("QuickPickDialog deferred accepts", () => {
  /** A result the test settles by hand, as a row's details loading would. */
  function deferred() {
    let resolve: (result: QuickPickAcceptResult) => void = () => undefined;
    let reject: (error: Error) => void = () => undefined;
    const promise = new Promise<QuickPickAcceptResult>((resolvePromise, rejectPromise) => {
      resolve = resolvePromise;
      reject = rejectPromise;
    });
    return { promise, resolve, reject };
  }

  let setOpen: (open: boolean) => void = () => undefined;
  function Deferring({
    accept,
    onCancel,
  }: {
    accept: () => QuickPickDialogAcceptResult;
    onCancel?: () => void;
  }) {
    const [open, setOpenState] = useState(true);
    setOpen = setOpenState;
    return (
      <QuickPickDialog
        open={open}
        onOpenChange={setOpenState}
        onCancel={onCancel}
        label="Pick"
        segments={commands}
        query=""
        onQueryChange={() => undefined}
        emptyMessage="No results found"
        onAccept={accept}
      />
    );
  }
  const dialog = () => document.querySelector("[data-quick-pick-dialog]");
  const settleWith = async (settle: () => void) => {
    await act(async () => {
      settle();
      await Promise.resolve();
    });
  };

  it("waits open, then closes as an accept and runs its action once closed, without a cancel", async () => {
    const onCancel = vi.fn();
    const afterClose = vi.fn(() => expect(dialog()).toBeNull());
    const load = deferred();
    render(<Deferring accept={() => load.promise} onCancel={onCancel} />);
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await settleWith(() => load.resolve({ afterClose }));
    await waitFor(() => expect(afterClose).toHaveBeenCalledOnce());
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("stays open when the result keeps it open or fails", async () => {
    const results = [deferred(), deferred()];
    let next = 0;
    render(<Deferring accept={() => results[next++]!.promise} />);
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    await settleWith(() => results[0]!.resolve("keepOpen"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await userEvent.keyboard("{Enter}");
    await settleWith(() => results[1]!.reject(new Error("Couldn't load the row.")));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("drops a result that settles after Escape and a reopen, keeping the new opening", async () => {
    const elsewhere = document.createElement("input");
    document.body.append(elsewhere);
    onTestFinished(() => elsewhere.remove());
    const abandoned = vi.fn(() => elsewhere.focus());
    const load = deferred();
    render(<Deferring accept={() => load.promise} />);
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(dialog()).toBeNull());
    act(() => setOpen(true));
    const reopened = await screen.findByRole("combobox", { name: "Pick" });
    await waitFor(() => expect(reopened).toHaveFocus());

    await settleWith(() => load.resolve({ afterClose: abandoned }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(reopened).toHaveFocus();
    expect(abandoned).not.toHaveBeenCalled();
  });

  it("drops a result that settles after Escape, before the next opening", async () => {
    const abandoned = vi.fn();
    const load = deferred();
    render(<Deferring accept={() => load.promise} />);
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(dialog()).toBeNull());
    await settleWith(() => load.resolve({ afterClose: abandoned }));
    act(() => setOpen(true));
    await screen.findByRole("combobox", { name: "Pick" });
    await settleWith(() => undefined);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(abandoned).not.toHaveBeenCalled();
  });

  it("drops a result that settles after the picker was closed from outside", async () => {
    const abandoned = vi.fn();
    const load = deferred();
    render(<Deferring accept={() => load.promise} />);
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    // Closed by its owner, such as by the shortcut that opened it, not by an accept.
    act(() => setOpen(false));
    await waitFor(() => expect(dialog()).toBeNull());
    await settleWith(() => load.resolve({ afterClose: abandoned }));
    act(() => setOpen(true));
    await screen.findByRole("combobox", { name: "Pick" });
    await settleWith(() => undefined);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(abandoned).not.toHaveBeenCalled();
  });

  it("acts only on the latest of two accepts, whichever settles first", async () => {
    const first = deferred();
    const second = deferred();
    const results = [first, second];
    let next = 0;
    const firstAction = vi.fn();
    const secondAction = vi.fn();
    render(<Deferring accept={() => results[next++]!.promise} />);
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    await userEvent.keyboard("{ArrowDown}{Enter}");
    await settleWith(() => first.resolve({ afterClose: firstAction }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await settleWith(() => second.resolve({ afterClose: secondAction }));
    await waitFor(() => expect(secondAction).toHaveBeenCalledOnce());
    expect(firstAction).not.toHaveBeenCalled();
  });

  it("keeps each opening's accepts its own across a finished close, a cancel and a reopen", async () => {
    // The first opening's accept closed and ran its action; the second's is cancelled, so
    // its result settling in the third opening neither closes it nor runs anything again.
    const load = deferred();
    const action = vi.fn();
    let accepts = 0;
    render(
      <Deferring
        accept={() => {
          accepts += 1;
          return accepts === 1 ? { afterClose: action } : load.promise;
        }}
      />,
    );
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(action).toHaveBeenCalledOnce());
    act(() => setOpen(true));
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(dialog()).toBeNull());
    act(() => setOpen(true));
    await screen.findByRole("combobox", { name: "Pick" });
    await settleWith(() => load.resolve(undefined));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(action).toHaveBeenCalledOnce();
  });

  it("does nothing once unmounted", async () => {
    const onOpenChange = vi.fn();
    const load = deferred();
    const { unmount } = render(
      <QuickPickDialog
        open
        onOpenChange={onOpenChange}
        label="Pick"
        segments={commands}
        query=""
        onQueryChange={() => undefined}
        emptyMessage="No results found"
        onAccept={() => load.promise}
      />,
    );
    await screen.findByRole("combobox", { name: "Pick" });
    await userEvent.keyboard("{Enter}");
    unmount();
    await settleWith(() => load.resolve(undefined));
    expect(onOpenChange).not.toHaveBeenCalled();
  });
});
