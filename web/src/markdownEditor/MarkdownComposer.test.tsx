import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { keyOwnerOf } from "@/lib/shortcuts/editorOwnedChords";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import { MarkdownComposer, type MarkdownComposerProps } from "./MarkdownComposer";
import { resetSettledUploadsForTests, resetUploadIdsForTests } from "./pullRequestAttachments";

// The PR editors' completion reads go through react-query, as in the app.
const queryClient = new QueryClient();

const upload = vi.hoisted(() => ({
  // Every upload still running, oldest first, to settle together or one at a time.
  pending: [] as { resolve: (value: unknown) => void; reject: (error: unknown) => void }[],
}));
vi.mock("@/newPullRequestPage/hooks/usePullRequests", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  uploadPullRequestAttachment: () =>
    new Promise((resolve, reject) => {
      upload.pending.push({ resolve, reject });
    }),
}));

const ASSET = "https://github.com/user-attachments/assets";

beforeEach(() => {
  resetUploadIdsForTests();
  resetSettledUploadsForTests();
});
afterEach(() => {
  upload.pending = [];
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
});

interface Harness {
  /** The draft's text as its owner holds it, including changes made while unmounted. */
  text: () => string;
  onSubmit: ReturnType<typeof vi.fn<() => void>>;
  onCancel: ReturnType<typeof vi.fn<() => void>>;
  uploading: () => number;
  errors: string[];
  unmount: () => void;
}

function mount(initial: string, props: Partial<MarkdownComposerProps> = {}): Harness {
  // The owner's draft outlives the composer, as the page's draft stores do.
  const store = { text: initial, uploading: 0 };
  const listeners = new Set<() => void>();
  const update = (change: (text: string) => string) => {
    store.text = change(store.text);
    listeners.forEach((listener) => listener());
  };
  const harness: Harness = {
    text: () => store.text,
    onSubmit: vi.fn<() => void>(),
    onCancel: vi.fn<() => void>(),
    uploading: () => store.uploading,
    errors: [],
    unmount: () => {},
  };
  function Host() {
    const [, rerender] = useState(0);
    useState(() => listeners.add(() => rerender((count) => count + 1)));
    return (
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <MarkdownComposer
            value={store.text}
            onChange={(next) => update(() => next)}
            onSubmit={harness.onSubmit}
            onCancel={harness.onCancel}
            submitLabel="Comment"
            submitVerb="comment"
            size="comment"
            label="Comment"
            preview={<p data-testid="rendered">{store.text}</p>}
            attachments={{
              conversationId: "conv_1",
              prUrl: "https://github.com/acme/app/pull/1",
              rememberUploadedImage: async () => {},
              updateDraftText: update,
              onUploadingChange: (delta) => {
                store.uploading += delta;
              },
              onAttachmentError: (error) => harness.errors.push(error),
            }}
            {...props}
          />
        </TooltipProvider>
      </QueryClientProvider>
    );
  }
  const { unmount } = render(<Host />);
  harness.unmount = unmount;
  return harness;
}

const textarea = () => screen.getByRole("textbox", { name: "Comment" }) as HTMLTextAreaElement;

function select(start: number, end = start): void {
  const element = textarea();
  act(() => element.focus());
  element.setSelectionRange(start, end);
}

function pasteFiles(element: HTMLElement, ...names: string[]): void {
  const files = names.map((name) => new File(["png"], name, { type: "image/png" }));
  fireEvent.paste(element, { clipboardData: { files, getData: () => "" } });
}

async function finishUpload(index: number, url: string): Promise<void> {
  await act(async () => {
    upload.pending[index]!.resolve({ url, content_type: "image/png" });
  });
}

/** Composers that edit one draft, as the Overview's pinned box and the Comments tab do. */
function mountSharedDraft(labels: string[], initial = ""): { text: string } {
  const store = { text: initial };
  const listeners = new Set<() => void>();
  const update = (change: (text: string) => string) => {
    store.text = change(store.text);
    listeners.forEach((listener) => listener());
  };
  function Shared({ label }: { label: string }) {
    const [, rerender] = useState(0);
    useState(() => listeners.add(() => rerender((count) => count + 1)));
    return (
      <MarkdownComposer
        value={store.text}
        onChange={(next) => update(() => next)}
        onSubmit={() => {}}
        submitLabel="Comment"
        submitVerb="comment"
        size="comment"
        label={label}
        preview={null}
        attachments={{
          conversationId: "conv_1",
          prUrl: "https://github.com/acme/app/pull/1",
          rememberUploadedImage: async () => {},
          updateDraftText: update,
          onUploadingChange: () => {},
          onAttachmentError: () => {},
        }}
      />
    );
  }
  render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        {labels.map((label) => (
          <Shared key={label} label={label} />
        ))}
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return store;
}

const sharedTextarea = (label: string) =>
  screen.getByRole("textbox", { name: label }) as HTMLTextAreaElement;

describe("MarkdownComposer", () => {
  it("applies a toolbar format to the selection and keeps it selected", () => {
    const harness = mount("make this bold");
    select(5, 9);
    fireEvent.click(screen.getByRole("button", { name: "Bold" }));
    expect(harness.text()).toBe("make **this** bold");
    expect([textarea().selectionStart, textarea().selectionEnd]).toEqual([7, 11]);
  });

  it("shows the reference's nine toolbar buttons, and only while writing", () => {
    mount("");
    const toolbar = screen.getByRole("toolbar", { name: "Formatting" });
    expect(
      Array.from(toolbar.querySelectorAll("button")).map((button) =>
        button.getAttribute("aria-label"),
      ),
    ).toEqual([
      "Heading",
      "Bold",
      "Italic",
      "Inline Code",
      "Link",
      "Quote",
      "Bulleted List",
      "Numbered List",
      "Task List",
    ]);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));
    expect(screen.queryByRole("toolbar", { name: "Formatting" })).toBeNull();
  });

  it("switches to Preview and back with ⇧⌘P, keeping the same textarea and its text", () => {
    const harness = mount("**hi**", { onCancel: undefined });
    const before = textarea();
    select(0);
    fireEvent.keyDown(before, {
      key: "P",
      code: "KeyP",
      ctrlKey: true,
      shiftKey: true,
    });
    expect(screen.getByRole("tab", { name: "Preview" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("rendered")).toHaveTextContent("**hi**");
    // Write stays mounted, so its native undo history survives the switch.
    expect(before.isConnected).toBe(true);
    fireEvent.keyDown(screen.getByRole("tabpanel", { name: "Preview" }), {
      key: "P",
      code: "KeyP",
      ctrlKey: true,
      shiftKey: true,
    });
    expect(screen.getByRole("tab", { name: "Write" })).toHaveAttribute("aria-selected", "true");
    expect(textarea()).toBe(before);
    expect(harness.text()).toBe("**hi**");
  });

  it("moves focus into Preview on ⇧⌘P, so Preview's own keys work", async () => {
    const harness = mount("text");
    select(0);
    fireEvent.keyDown(textarea(), { key: "P", code: "KeyP", ctrlKey: true, shiftKey: true });
    await act(async () => {
      await new Promise((resolve) => {
        requestAnimationFrame(() => resolve(undefined));
      });
    });
    const panel = screen.getByRole("tabpanel", { name: "Preview" });
    expect(panel.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(harness.onCancel).toHaveBeenCalledTimes(1);
  });

  it("says there is nothing to preview for an empty draft", () => {
    mount("  ");
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));
    expect(screen.getByText("Nothing to preview yet.")).toBeInTheDocument();
  });

  it("submits on ⌘↵ and cancels on Esc", () => {
    const harness = mount("text");
    select(4);
    fireEvent.keyDown(textarea(), { key: "Enter", ctrlKey: true });
    expect(harness.onSubmit).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(textarea(), { key: "Escape" });
    expect(harness.onCancel).toHaveBeenCalledTimes(1);
  });

  it("keeps a disabled composer's Preview from cancelling or submitting", () => {
    const harness = mount("Saving this", { disabled: true });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));
    const preview = screen.getByRole("tabpanel", { name: "Preview" });
    fireEvent.keyDown(preview, { key: "Escape" });
    fireEvent.keyDown(preview, { key: "Enter", ctrlKey: true });
    expect(harness.onCancel).not.toHaveBeenCalled();
    expect(harness.onSubmit).not.toHaveBeenCalled();
    // Switching back to Write still works, to read the text while it saves.
    fireEvent.keyDown(preview, { key: "P", code: "KeyP", ctrlKey: true, shiftKey: true });
    expect(screen.getByRole("tab", { name: "Write" })).toHaveAttribute("aria-selected", "true");
  });

  it("owns the editor chords, so ⌘B formats instead of toggling the sidebar", () => {
    mount("");
    expect(keyOwnerOf(textarea())).toBe("rich");
  });

  it("links the selection when a URL is pasted over it", () => {
    const harness = mount("see the docs");
    select(4, 12);
    fireEvent.paste(textarea(), {
      clipboardData: { files: [], getData: () => "https://example.com/docs" },
    });
    expect(harness.text()).toBe("see [the docs](https://example.com/docs)");
  });

  it("pastes an attachment as a placeholder that becomes the upload", async () => {
    const harness = mount("Before ");
    select(7);
    pasteFiles(textarea(), "shot.png");
    expect(harness.text()).toBe("Before ![Uploading shot.png…](#upload-1)");
    expect(harness.uploading()).toBe(1);
    // Typing meanwhile stays where it is.
    fireEvent.change(textarea(), { target: { value: `${harness.text()} after` } });
    await finishUpload(0, `${ASSET}/1`);
    expect(harness.text()).toBe(`Before ![shot](${ASSET}/1) after`);
    expect(harness.uploading()).toBe(0);
  });

  it("lands two uploads that finish together", async () => {
    const harness = mount("");
    select(0);
    pasteFiles(textarea(), "a.png", "b.png");
    expect(harness.uploading()).toBe(2);
    await act(async () => {
      upload.pending[1]!.resolve({ url: `${ASSET}/b`, content_type: "image/png" });
      upload.pending[0]!.resolve({ url: `${ASSET}/a`, content_type: "image/png" });
    });
    expect(harness.text()).toBe(`![a](${ASSET}/a)![b](${ASSET}/b)`);
    expect(harness.uploading()).toBe(0);
  });

  it("removes a failed upload's placeholder and reports why", async () => {
    const harness = mount("");
    select(0);
    pasteFiles(textarea(), "a.png");
    await act(async () => upload.pending[0]!.reject(new Error("Too many requests")));
    expect(harness.text()).toBe("");
    expect(harness.errors).toEqual(["Couldn’t attach a.png: Too many requests"]);
  });

  it("refuses files that aren't images or videos before uploading", () => {
    const harness = mount("");
    select(0);
    fireEvent.paste(textarea(), {
      clipboardData: {
        files: [new File(["x"], "notes.txt", { type: "text/plain" })],
        getData: () => "",
      },
    });
    expect(upload.pending).toHaveLength(0);
    expect(harness.errors).toEqual(["Only images and videos can be attached."]);
  });

  it("lands an upload in the draft after the composer closed", async () => {
    const harness = mount("");
    select(0);
    pasteFiles(textarea(), "a.png");
    harness.unmount();
    await finishUpload(0, `${ASSET}/a`);
    expect(harness.text()).toBe(`![a](${ASSET}/a)`);
    expect(harness.uploading()).toBe(0);
  });

  it("posts a settled upload that Undo brings back as its placeholder, without resetting the text", async () => {
    const harness = mount("");
    select(0);
    pasteFiles(textarea(), "a.png");
    const placeholder = harness.text();
    await finishUpload(0, `${ASSET}/a`);
    // Undo brings the placeholder back as an ordinary edit; the textarea keeps what the browser
    // made, so its undo history stays usable, while the draft keeps the upload.
    fireEvent.change(textarea(), { target: { value: `${placeholder} more` } });
    expect(textarea().value).toBe(`${placeholder} more`);
    expect(harness.text()).toBe(`![a](${ASSET}/a) more`);
  });

  it("keeps an edited textarea's text when an upload settles off the keyboard, until it's focused", async () => {
    const harness = mount("");
    select(0);
    fireEvent.change(textarea(), { target: { value: "Shot: " } });
    textarea().setSelectionRange(6, 6);
    pasteFiles(textarea(), "a.png");
    // Preview takes the keyboard while the upload runs.
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));
    act(() => textarea().blur());
    await finishUpload(0, `${ASSET}/a`);
    expect(harness.text()).toBe(`Shot: ![a](${ASSET}/a)`);
    expect(screen.getByTestId("rendered")).toHaveTextContent(`Shot: ![a](${ASSET}/a)`);
    expect(textarea().value).toBe("Shot: ![Uploading a.png…](#upload-1)");
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Write" }));
    act(() => textarea().focus());
    expect(textarea().value).toBe(`Shot: ![a](${ASSET}/a)`);
  });

  it("keeps an edited composer's history when another composer of its draft takes the upload", async () => {
    // The Overview's pinned box and the Comments tab edit one draft; both stay mounted.
    const store = mountSharedDraft(["Pinned", "Comment"]);
    select(0);
    fireEvent.change(textarea(), { target: { value: "Shot: " } });
    textarea().setSelectionRange(6, 6);
    pasteFiles(textarea(), "a.png");
    act(() => textarea().blur());
    const placeholder = "Shot: ![Uploading a.png…](#upload-1)";
    expect(screen.getByRole("textbox", { name: "Pinned" })).toHaveValue(placeholder);
    await finishUpload(0, `${ASSET}/a`);
    expect(store.text).toBe(`Shot: ![a](${ASSET}/a)`);
    // The edited composer's text wasn't set from code; focusing it swaps the upload in.
    expect(textarea().value).toBe(placeholder);
    act(() => textarea().focus());
    expect(textarea().value).toBe(`Shot: ![a](${ASSET}/a)`);
  });

  it("doesn't bring back text another composer deleted, when the draft returns to an earlier text", () => {
    const store = mountSharedDraft(["Comment", "Pinned"]);
    fireEvent.change(sharedTextarea("Comment"), { target: { value: "x" } });
    fireEvent.change(sharedTextarea("Pinned"), { target: { value: "xy" } });
    expect(sharedTextarea("Comment").value).toBe("xy");
    // The other composer deletes what it added, so the draft is "x" again.
    fireEvent.change(sharedTextarea("Pinned"), { target: { value: "x" } });
    expect(store.text).toBe("x");
    expect(sharedTextarea("Comment").value).toBe("x");
    fireEvent.change(sharedTextarea("Comment"), { target: { value: "xz" } });
    expect(store.text).toBe("xz");
    expect(sharedTextarea("Comment").value).toBe("xz");
    expect(sharedTextarea("Pinned").value).toBe("xz");
  });

  it("shows an upload at once in a textarea nobody edited here", async () => {
    const harness = mount("![Uploading a.png…](#upload-1)");
    // An upload this composer didn't start, from the draft's earlier composer.
    const { recordSettledUpload, deliverUpload } = await import("./pullRequestAttachments");
    act(() => {
      recordSettledUpload("upload-1", `![a](${ASSET}/a)`);
      expect(deliverUpload("upload-1", `![a](${ASSET}/a)`)).toBe(true);
    });
    expect(textarea().value).toBe(`![a](${ASSET}/a)`);
    expect(harness.text()).toBe(`![a](${ASSET}/a)`);
  });

  it("swaps a placeholder Undo brought back for its upload when the textarea is focused again", async () => {
    const harness = mount("");
    select(0);
    pasteFiles(textarea(), "a.png");
    const placeholder = harness.text();
    await finishUpload(0, `${ASSET}/a`);
    fireEvent.change(textarea(), { target: { value: placeholder } });
    act(() => textarea().blur());
    act(() => textarea().focus());
    expect(textarea().value).toBe(`![a](${ASSET}/a)`);
    expect(harness.text()).toBe(`![a](${ASSET}/a)`);
  });

  it("keeps typed text and the caret while its owner re-renders a moment later", async () => {
    // A draft store that, like React Query, tells its readers about a change on a later tick.
    const store = { text: "ac" };
    function LateOwner() {
      const [shown, setShown] = useState(store.text);
      return (
        <QueryClientProvider client={queryClient}>
          <TooltipProvider>
            <MarkdownComposer
              value={shown}
              onChange={(next) => {
                store.text = next;
                setTimeout(() => setShown(store.text), 0);
              }}
              onSubmit={() => {}}
              submitLabel="Comment"
              submitVerb="comment"
              size="comment"
              label="Comment"
              preview={null}
              attachments={null}
            />
          </TooltipProvider>
        </QueryClientProvider>
      );
    }
    render(<LateOwner />);
    select(1);
    fireEvent.change(textarea(), { target: { value: "abc", selectionStart: 2, selectionEnd: 2 } });
    // Until the owner re-renders, the textarea must not fall back to the older text.
    expect(textarea().value).toBe("abc");
    expect(textarea().selectionStart).toBe(2);
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
    });
    expect(textarea().value).toBe("abc");
    expect(store.text).toBe("abc");
  });

  it("offers every format once in ⌘K, and runs a row on the textarea's selection", () => {
    const harness = mount("plain words");
    select(0, 5);
    const rows = Object.values(usePaletteCommandsStore.getState().groups).flatMap(
      (group) => group.commands,
    );
    const labels = rows.map((row) => row.label);
    expect(labels).toEqual([
      "Heading",
      "Bold",
      "Italic",
      "Inline Code",
      "Link",
      "Quote",
      "Bulleted List",
      "Numbered List",
      "Task List",
      "Strikethrough",
      "Code Block",
      "Attach Images or Videos…",
      "Show Preview",
      "Comment",
    ]);
    act(() => rows.find((row) => row.label === "Bold")!.run());
    expect(harness.text()).toBe("**plain** words");
    act(() => rows.find((row) => row.label === "Comment")!.run());
    expect(harness.onSubmit).toHaveBeenCalledTimes(1);
  });
});
