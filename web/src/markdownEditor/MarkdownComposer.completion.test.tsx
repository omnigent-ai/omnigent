import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

import type { PullRequestCandidate } from "./markdownCompletion";
import {
  MarkdownCompletionProvider,
  type MarkdownCompletionSource,
} from "./markdownCompletionContext";
import { MarkdownComposer } from "./MarkdownComposer";

// Whom the PR's reviewer picker would list, and what a `#` search finds.
const reads = vi.hoisted(() => ({
  pickerOpened: [] as boolean[],
  searched: [] as PullRequestCandidate[],
  searching: false,
  typed: [] as (string | null)[],
  // Beyond the default people, as a large organization's list would add.
  more: [] as { login: string; kind: "user"; name: string }[],
}));
vi.mock("@/shell/ReviewerPickerList", () => ({
  useReviewerPeopleSource: (_conversationId: string, _prUrl: string, open: boolean) => {
    reads.pickerOpened.push(open);
    return {
      assignable: [
        { login: "octocat", kind: "user", name: "Octo Cat" },
        { login: "web", kind: "team", name: "Web" },
        ...reads.more,
      ],
      members: [],
      requested: [],
      recent: [],
      pending: false,
      notes: [],
    };
  },
}));
vi.mock("./pullRequestCompletionSearch", () => ({
  usePullRequestCompletions: (_prUrl: string | null, typed: string | null) => {
    reads.typed.push(typed);
    return { pullRequests: typed === null ? [] : reads.searched, searching: reads.searching };
  },
}));

const PAGE = "https://github.com/acme/app/pull/1";
const SOURCE: MarkdownCompletionSource = {
  conversationId: "conv_1",
  prUrl: PAGE,
  participants: [{ login: "olivia", kind: "user", name: null }],
  pullRequests: [
    { url: PAGE, number: 1, title: "Add the parser", state: "OPEN", isDraft: false },
    {
      url: `${PAGE.slice(0, -1)}2`,
      number: 2,
      title: "Use the parser",
      state: "MERGED",
      isDraft: false,
    },
  ],
};

beforeEach(() => {
  Object.assign(reads, { pickerOpened: [], searched: [], searching: false, typed: [], more: [] });
});
afterEach(cleanup);

function mount(initial: string, source: MarkdownCompletionSource | null = SOURCE) {
  const store = { text: initial };
  const harness = {
    text: () => store.text,
    onSubmit: vi.fn<() => void>(),
    onCancel: vi.fn<() => void>(),
  };
  function Host() {
    const [text, setText] = useState(initial);
    return (
      <TooltipProvider>
        <MarkdownCompletionProvider source={source}>
          <MarkdownComposer
            value={text}
            onChange={(next) => {
              store.text = next;
              setText(next);
            }}
            onSubmit={harness.onSubmit}
            onCancel={harness.onCancel}
            submitLabel="Comment"
            submitVerb="comment"
            size="comment"
            label="Comment"
            preview={<p data-testid="rendered">{text}</p>}
            attachments={null}
          />
        </MarkdownCompletionProvider>
      </TooltipProvider>
    );
  }
  render(<Host />);
  return harness;
}

// A textbox while closed and a combobox while the list shows, so found by its label.
const textarea = () =>
  screen.getByLabelText("Comment", { selector: "textarea" }) as HTMLTextAreaElement;
const menu = () => screen.queryByTestId("markdown-completion");
const options = () =>
  within(screen.getByTestId("markdown-completion"))
    .getAllByRole("option")
    .map((option) => option.textContent);

/** Types `value` as the textarea's whole text, with the caret at its end or `caret`. */
function type(value: string, caret = value.length): void {
  const element = textarea();
  act(() => element.focus());
  fireEvent.change(element, { target: { value } });
  moveCaret(caret);
}

/** Moves the caret or selection as an arrow key does: React reads it on the key's release. */
function moveCaret(start: number, end = start): void {
  textarea().setSelectionRange(start, end);
  fireEvent.keyUp(textarea(), { key: "ArrowLeft" });
}

const key = (init: KeyboardEventInit & { key: string }) => fireEvent.keyDown(textarea(), init);

describe("MarkdownComposer completion", () => {
  it("lists people for `@` and puts the pick in place with a space after it", async () => {
    const harness = mount("");
    type("Thanks @oc");
    expect(options()).toEqual(["@octocatOcto Cat"]);
    expect(textarea()).toHaveAttribute("role", "combobox");
    expect(textarea()).toHaveAttribute("aria-expanded", "true");
    const option = within(menu()!).getByRole("option");
    await waitFor(() => expect(textarea()).toHaveAttribute("aria-activedescendant", option.id));
    expect(textarea()).toHaveAttribute("aria-controls", within(menu()!).getByRole("listbox").id);

    key({ key: "Enter" });
    expect(harness.text()).toBe("Thanks @octocat ");
    expect(textarea().selectionStart).toBe("Thanks @octocat ".length);
    expect(harness.onSubmit).not.toHaveBeenCalled();
    expect(menu()).toBeNull();
    expect(textarea()).not.toHaveAttribute("role");
  });

  it("names a team with its organization, and the PR's own people first", () => {
    const harness = mount("");
    type("@");
    expect(options()[0]).toBe("@olivia");
    type("@we");
    expect(options()).toEqual(["@acme/webWeb"]);
    key({ key: "Tab" });
    expect(harness.text()).toBe("@acme/web ");
  });

  it("puts the PR's people ahead of closer matches from the organization, but not the writer", () => {
    reads.more = [
      { login: "girija-c_data", kind: "user", name: "Girija C" },
      { login: "gilad-lev_data", kind: "user", name: "Gilad Lev" },
    ];
    mount("", {
      ...SOURCE,
      participants: [
        { login: "gilles-peiffer_data", kind: "user", name: null },
        { login: "gina-yuan_data", kind: "user", name: null },
      ],
      viewer: "Gina-Yuan_data",
    });
    type("@gi");
    expect(options()).toEqual([
      "@gilles-peiffer_data",
      "@girija-c_dataGirija C",
      "@gilad-lev_dataGilad Lev",
      "@gina-yuan_data",
    ]);
  });

  it("moves through the list with the arrows, wrapping at each end", () => {
    const harness = mount("");
    type("@");
    expect(options()).toEqual(["@olivia", "@octocatOcto Cat", "@acme/webWeb"]);
    key({ key: "ArrowUp" });
    key({ key: "ArrowDown" });
    key({ key: "ArrowDown" });
    key({ key: "Enter" });
    expect(harness.text()).toBe("@octocat ");
  });

  it("reads the picker's people only once an `@` is typed", () => {
    mount("");
    type("Hello #");
    expect(reads.pickerOpened.every((open) => !open)).toBe(true);
    type("Hello @");
    expect(reads.pickerOpened.at(-1)).toBe(true);
  });

  it("lists the PRs the view read for `#`, then what the search finds", () => {
    reads.searched = [
      {
        url: "https://github.com/acme/app/pull/42",
        number: 42,
        title: "Fix flaky test",
        state: "OPEN",
        isDraft: true,
      },
    ];
    const harness = mount("Stacked on ");
    type("Stacked on #");
    expect(options()).toEqual(["#1Add the parser", "#2Use the parser", "#42Fix flaky test"]);
    type("Stacked on #4");
    expect(options()).toEqual(["#42Fix flaky test"]);
    key({ key: "Enter" });
    expect(harness.text()).toBe("Stacked on #42 ");
  });

  it("says a search is running, then that nothing matched", () => {
    reads.searching = true;
    mount("");
    type("#999");
    expect(within(menu()!).getByText("Searching…")).toBeInTheDocument();
    reads.searching = false;
    type("#9999");
    expect(within(menu()!).getByText("No pull requests match")).toBeInTheDocument();
  });

  it("closes on Escape before Escape cancels, and stays closed for that token", () => {
    const harness = mount("");
    type("@oc");
    key({ key: "Escape" });
    expect(menu()).toBeNull();
    expect(harness.onCancel).not.toHaveBeenCalled();
    type("@oct");
    expect(menu()).toBeNull();
    key({ key: "Escape" });
    expect(harness.onCancel).toHaveBeenCalledTimes(1);
    // A new token opens again.
    type("@oct and @");
    expect(menu()).not.toBeNull();
  });

  it("leaves ⌘↵, formatting chords and a composing IME's keys to the editor", () => {
    const harness = mount("");
    type("@oc");
    key({ key: "Enter", ctrlKey: true });
    expect(harness.onSubmit).toHaveBeenCalledTimes(1);
    key({ key: "Enter", isComposing: true });
    expect(harness.text()).toBe("@oc");
    key({ key: "b", code: "KeyB", ctrlKey: true });
    expect(harness.text()).toBe("@oc**bold text**");
  });

  it("leaves Enter to the editor in inline code and on code lines", () => {
    const harness = mount("");
    for (const text of ["`@oc", "see `a`, `@oc", "    @oc", "```@oc"]) {
      type(text);
      expect(menu(), text).toBeNull();
      expect(key({ key: "Enter" }), text).toBe(true);
      expect(harness.text()).toBe(text);
    }
  });

  it("leaves Tab and Enter alone while nothing completes", () => {
    const harness = mount("");
    type("email me@ex");
    expect(menu()).toBeNull();
    const tab = key({ key: "Tab" });
    expect(tab).toBe(true);
    expect(harness.text()).toBe("email me@ex");
  });

  it("closes on a caret move out of the token, on blur and on Preview", () => {
    mount("");
    type("@oc and more", 3);
    expect(menu()).not.toBeNull();
    type("@oc and more", 8);
    expect(menu()).toBeNull();

    type("@oc");
    act(() => textarea().blur());
    expect(menu()).toBeNull();

    type("@oc");
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));
    expect(menu()).toBeNull();
  });

  it("picks with the pointer without moving the caret first", () => {
    const harness = mount("");
    type("Hi @oc");
    const option = within(menu()!).getByRole("option");
    expect(fireEvent.mouseDown(option)).toBe(false);
    fireEvent.mouseUp(option);
    fireEvent.click(option);
    expect(harness.text()).toBe("Hi @octocat ");
  });

  it("still links a pasted URL over a selection while the list showed", () => {
    const harness = mount("");
    type("see the docs @oc");
    moveCaret(4, 12);
    expect(menu()).toBeNull();
    fireEvent.paste(textarea(), {
      clipboardData: { files: [], getData: () => "https://example.com/docs" },
    });
    expect(harness.text()).toBe("see [the docs](https://example.com/docs) @oc");
  });

  it("completes nothing outside a PR's editors", () => {
    const harness = mount("", null);
    type("@oc");
    expect(menu()).toBeNull();
    key({ key: "Enter" });
    expect(harness.text()).toBe("@oc");
    expect(reads.typed.every((typed) => typed === null)).toBe(true);
  });
});
