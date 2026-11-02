// Keyboard driving for AskUserQuestion forms: Enter advances or submits, digits
// pick options, Escape cancels. Keys pressed outside a form reach it only when it
// is keyboardActive, and never while a text field, composer menu, dialog, or other
// focused control owns them.

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ClaudeQuestion } from "@/lib/askUserQuestion";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "@/lib/composerSendShortcutPreferences";
import { EmbeddedProvider } from "@/lib/embedded";
import { setEmbedRoot } from "@/lib/host";
import { ApprovalCard } from "./ApprovalCard";
import { AskUserQuestionForm } from "./AskUserQuestionForm";

const framework: ClaudeQuestion = {
  id: "framework",
  question: "Which framework?",
  header: "Framework",
  multiSelect: false,
  options: [{ label: "React" }, { label: "Vue" }],
};
const snacks: ClaudeQuestion = {
  id: "snacks",
  question: "Pick snacks",
  multiSelect: true,
  options: [{ label: "Popcorn" }, { label: "Pretzels" }],
};
const color: ClaudeQuestion = {
  id: "color",
  question: "Pick a color",
  multiSelect: false,
  options: [{ label: "Red" }, { label: "Blue" }],
};

// Each form gets its own elicitation, so no test restores another's unsent draft.
let elicitationCount = 0;
const nextElicitationId = () => `elic_${++elicitationCount}`;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
});

/** Renders the form; `beside` is rendered after it, outside the form's root. */
function renderForm(
  questions: ClaudeQuestion[],
  options: { keyboardActive?: boolean; beside?: ReactNode } = {},
) {
  const onSubmit = vi.fn();
  const onReject = vi.fn();
  render(
    <>
      <AskUserQuestionForm
        elicitationId={nextElicitationId()}
        questions={questions}
        onSubmit={onSubmit}
        onReject={onReject}
        keyboardActive={options.keyboardActive ?? true}
      />
      {options.beside}
    </>,
  );
  return { onSubmit, onReject };
}

/** A composer card shaped like the chat composer; `sideChat` marks the side-chat composer. */
function composerCard({
  text = "",
  hasDraft = false,
  sideChat = false,
  suggestionsOpen = false,
}: {
  text?: string;
  hasDraft?: boolean;
  sideChat?: boolean;
  suggestionsOpen?: boolean;
} = {}) {
  return (
    <div
      data-composer-card=""
      data-side-chat={sideChat ? "true" : undefined}
      data-suggestions-open={suggestionsOpen ? "true" : undefined}
    >
      <textarea
        aria-label="Message the agent"
        defaultValue={text}
        data-has-draft={hasDraft ? "true" : undefined}
      />
    </div>
  );
}

const progress = () => screen.getByTestId("ask-user-question-progress").textContent;
const pressOnPage = (init: KeyboardEventInit) => fireEvent.keyDown(document.body, init);
const keyHints = () => document.querySelectorAll('[data-slot="shortcut-keys"]');

/** Stubs `window.matchMedia` so the named media queries match. */
function stubMedia(matches: (query: string) => boolean) {
  const matchMedia = window.matchMedia;
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    ...matchMedia(query),
    matches: matches(query),
  }));
}

describe("AskUserQuestionForm — Enter", () => {
  it("advances to the next question while answers are missing", () => {
    renderForm([framework, snacks]);
    pressOnPage({ key: "Enter" });
    expect(progress()).toBe("Question 2 of 2:");
  });

  it("submits from the last question once every question is answered", () => {
    const { onSubmit } = renderForm([framework, snacks]);
    fireEvent.click(screen.getByLabelText("React"));
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    fireEvent.click(screen.getByLabelText("Popcorn"));
    pressOnPage({ key: "Enter" });
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({ framework: "React", snacks: ["Popcorn"] });
  });

  it("submits from an earlier question when every answer is already in", () => {
    const { onSubmit } = renderForm([framework, snacks]);
    fireEvent.click(screen.getByLabelText("React"));
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    fireEvent.click(screen.getByLabelText("Popcorn"));
    fireEvent.click(screen.getByTestId("ask-user-question-prev"));
    expect(progress()).toBe("Question 1 of 2:");
    pressOnPage({ key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith({ framework: "React", snacks: ["Popcorn"] });
  });

  it("jumps from the last question back to the first unanswered one", () => {
    // Single-select questions start on their first option, so the multi-select is unanswered.
    renderForm([snacks, framework, color]);
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    pressOnPage({ key: "Enter" });
    expect(progress()).toBe("Question 1 of 3:");
  });

  it("inserts a newline on Shift+Enter in the custom input and advances on Enter", async () => {
    const user = userEvent.setup();
    renderForm([framework, snacks]);
    const custom = screen.getByTestId("ask-user-question-custom-input") as HTMLTextAreaElement;
    await user.click(custom);
    await user.keyboard("Ramen{Shift>}{Enter}{/Shift}Soba");
    expect(custom.value).toBe("Ramen\nSoba");
    expect(progress()).toBe("Question 1 of 2:");

    await user.keyboard("{Enter}");
    expect(custom.value).toBe("Ramen\nSoba");
    expect(progress()).toBe("Question 2 of 2:");
  });

  it("clicks a focused button once, without also advancing", async () => {
    const user = userEvent.setup();
    renderForm([framework, snacks, color]);
    screen.getByTestId("ask-user-question-next").focus();
    await user.keyboard("{Enter}");
    expect(progress()).toBe("Question 2 of 3:");
  });

  it("ignores IME composition and auto-repeat", () => {
    renderForm([framework, snacks]);
    pressOnPage({ key: "Enter", isComposing: true });
    pressOnPage({ key: "Enter", repeat: true });
    expect(progress()).toBe("Question 1 of 2:");
  });

  it.each([
    { name: "Ctrl", init: { ctrlKey: true } },
    { name: "Meta", init: { metaKey: true } },
    { name: "Alt", init: { altKey: true } },
    { name: "Shift", init: { shiftKey: true } },
  ])("ignores Enter with $name held", ({ init }) => {
    renderForm([framework, snacks]);
    pressOnPage({ key: "Enter", ...init });
    expect(progress()).toBe("Question 1 of 2:");
  });
});

describe("AskUserQuestionForm — Escape", () => {
  it("cancels from the custom input", () => {
    const { onReject } = renderForm([framework]);
    fireEvent.keyDown(screen.getByTestId("ask-user-question-custom-input"), { key: "Escape" });
    expect(onReject).toHaveBeenCalledTimes(1);
  });

  it("cancels with focus on the page", () => {
    const { onReject } = renderForm([framework]);
    pressOnPage({ key: "Escape" });
    expect(onReject).toHaveBeenCalledTimes(1);
  });

  it("leaves keys to an open /btw side answer, which Escape closes first", () => {
    const { onReject } = renderForm([framework, snacks], {
      beside: <div data-btw-overlay="true">Claude Quick Answer</div>,
    });
    expect(pressOnPage({ key: "Escape" })).toBe(true);
    expect(pressOnPage({ key: "Enter" })).toBe(true);
    expect(onReject).not.toHaveBeenCalled();
    expect(progress()).toBe("Question 1 of 2:");
  });

  it("leaves Escape to a button outside the form", () => {
    const { onReject } = renderForm([framework], {
      beside: <button type="button">Retry</button>,
    });
    const retry = screen.getByRole("button", { name: "Retry" });
    retry.focus();
    expect(fireEvent.keyDown(retry, { key: "Escape" })).toBe(true);
    expect(onReject).not.toHaveBeenCalled();
  });

  it("leaves Escape to an open panel that closes on it", () => {
    const { onReject } = renderForm([framework], { beside: <aside data-escape-owner="" /> });
    expect(pressOnPage({ key: "Escape" })).toBe(true);
    expect(onReject).not.toHaveBeenCalled();
  });

  it("leaves Enter and Escape to the composer's model/effort preview", () => {
    const { onReject } = renderForm([framework, snacks], {
      beside: (
        <>
          {composerCard()}
          <div data-cycle-preview="" />
        </>
      ),
    });
    const composer = screen.getByLabelText("Message the agent");
    composer.focus();
    expect(fireEvent.keyDown(composer, { key: "Enter" })).toBe(true);
    expect(fireEvent.keyDown(composer, { key: "Escape" })).toBe(true);
    expect(progress()).toBe("Question 1 of 2:");
    expect(onReject).not.toHaveBeenCalled();
  });

  it("embedded, takes keys only from inside the app", () => {
    const root = document.createElement("div");
    document.body.append(root);
    const hostButton = document.createElement("button");
    document.body.append(hostButton);
    setEmbedRoot(root);
    try {
      const onReject = vi.fn();
      render(
        <EmbeddedProvider>
          <AskUserQuestionForm
            elicitationId={nextElicitationId()}
            questions={[framework]}
            onSubmit={vi.fn()}
            onReject={onReject}
            keyboardActive
          />
        </EmbeddedProvider>,
        { container: root },
      );
      // A host-page control outside the app.
      expect(fireEvent.keyDown(hostButton, { key: "2" })).toBe(true);
      expect(fireEvent.keyDown(hostButton, { key: "Escape" })).toBe(true);
      expect(screen.getByLabelText("Vue")).not.toBeChecked();
      expect(onReject).not.toHaveBeenCalled();

      fireEvent.keyDown(root, { key: "2" });
      expect(screen.getByLabelText("Vue")).toBeChecked();
      fireEvent.keyDown(root, { key: "Escape" });
      expect(onReject).toHaveBeenCalledTimes(1);
    } finally {
      cleanup();
      setEmbedRoot(null);
      root.remove();
      hostButton.remove();
    }
  });

  it("leaves Escape to an open dialog that holds focus outside the form", () => {
    const onReject = vi.fn();
    render(
      <div role="dialog" aria-label="Settings">
        <button type="button">Close</button>
        <AskUserQuestionForm
          elicitationId={nextElicitationId()}
          questions={[framework]}
          onSubmit={vi.fn()}
          onReject={onReject}
          keyboardActive
        />
      </div>,
    );
    screen.getByRole("button", { name: "Close" }).focus();
    fireEvent.keyDown(screen.getByRole("button", { name: "Close" }), { key: "Escape" });
    expect(onReject).not.toHaveBeenCalled();
  });

  it("leaves Escape to the dialog when the form itself is inside it", () => {
    const onReject = vi.fn();
    render(
      <div role="dialog" aria-label="Settings">
        <AskUserQuestionForm
          elicitationId={nextElicitationId()}
          questions={[framework]}
          onSubmit={vi.fn()}
          onReject={onReject}
          keyboardActive
        />
      </div>,
    );
    const custom = screen.getByTestId("ask-user-question-custom-input");
    custom.focus();
    fireEvent.keyDown(custom, { key: "Escape" });
    expect(onReject).not.toHaveBeenCalled();
  });

  it("leaves Escape to an open menu", () => {
    const { onReject } = renderForm([framework], {
      beside: (
        <div role="menu">
          <button type="button" role="menuitem">
            Copy
          </button>
        </div>
      ),
    });
    const item = screen.getByRole("menuitem", { name: "Copy" });
    item.focus();
    fireEvent.keyDown(item, { key: "Escape" });
    expect(onReject).not.toHaveBeenCalled();
  });

  it("leaves Escape to another text field", () => {
    const { onReject } = renderForm([framework], {
      beside: <input aria-label="Search" />,
    });
    const search = screen.getByLabelText("Search");
    search.focus();
    fireEvent.keyDown(search, { key: "Escape" });
    expect(onReject).not.toHaveBeenCalled();
  });

  it("leaves Escape to open composer suggestions", () => {
    const { onReject } = renderForm([framework], {
      beside: composerCard({ suggestionsOpen: true }),
    });
    const composer = screen.getByLabelText("Message the agent");
    composer.focus();
    fireEvent.keyDown(composer, { key: "Escape" });
    expect(onReject).not.toHaveBeenCalled();
  });

  it("cancels from the composer when no suggestions are open", () => {
    const { onReject } = renderForm([framework], { beside: composerCard() });
    const composer = screen.getByLabelText("Message the agent");
    composer.focus();
    fireEvent.keyDown(composer, { key: "Escape" });
    expect(onReject).toHaveBeenCalledTimes(1);
  });

  it("ignores Escape during IME composition", () => {
    const { onReject } = renderForm([framework]);
    pressOnPage({ key: "Escape", isComposing: true });
    expect(onReject).not.toHaveBeenCalled();
  });
});

describe("AskUserQuestionForm — digits", () => {
  it("leaves digits to a side chat's focused control", () => {
    renderForm([framework], {
      beside: (
        <div data-side-chat-pane="">
          <button type="button">Side chat action</button>
        </div>
      ),
    });
    const sideAction = screen.getByRole("button", { name: "Side chat action" });
    sideAction.focus();
    expect(fireEvent.keyDown(sideAction, { key: "2" })).toBe(true);
    expect(fireEvent.keyDown(sideAction, { key: "3" })).toBe(true);
    expect(screen.getByLabelText("Vue")).not.toBeChecked();
    expect(sideAction).toHaveFocus();
  });

  it("selects option N of a single-select question", () => {
    renderForm([framework]);
    pressOnPage({ key: "2" });
    expect(screen.getByLabelText("Vue")).toBeChecked();
    pressOnPage({ key: "1" });
    expect(screen.getByLabelText("React")).toBeChecked();
    expect(screen.getByLabelText("Vue")).not.toBeChecked();
  });

  it("toggles an option of a multi-select question", () => {
    renderForm([snacks]);
    pressOnPage({ key: "1" });
    expect(screen.getByLabelText("Popcorn")).toBeChecked();
    pressOnPage({ key: "1" });
    expect(screen.getByLabelText("Popcorn")).not.toBeChecked();
    pressOnPage({ key: "2" });
    expect(screen.getByLabelText("Pretzels")).toBeChecked();
  });

  it("focuses the custom input and checks its row on the digit after the options", () => {
    renderForm([framework]);
    pressOnPage({ key: "3" });
    expect(document.activeElement).toBe(screen.getByTestId("ask-user-question-custom-input"));
    expect(screen.getByTestId("ask-user-question-custom-toggle")).toBeChecked();
  });

  it("does not take digits typed into the custom input", () => {
    renderForm([framework]);
    const custom = screen.getByTestId("ask-user-question-custom-input");
    custom.focus();
    expect(fireEvent.keyDown(custom, { key: "2" })).toBe(true);
    expect(screen.getByLabelText("Vue")).not.toBeChecked();
  });

  it("does not take digits typed into the composer", () => {
    renderForm([framework], { beside: composerCard() });
    const composer = screen.getByLabelText("Message the agent");
    composer.focus();
    expect(fireEvent.keyDown(composer, { key: "2" })).toBe(true);
    expect(screen.getByLabelText("Vue")).not.toBeChecked();
  });

  it("ignores digits beyond the last row", () => {
    renderForm([framework]);
    expect(pressOnPage({ key: "9" })).toBe(true);
    expect(screen.getByLabelText("React")).toBeChecked();
    expect(screen.getByLabelText("Vue")).not.toBeChecked();
  });
});

describe("AskUserQuestionForm — keys outside the form", () => {
  it("advances from an empty composer", () => {
    renderForm([framework, snacks], { beside: composerCard() });
    const composer = screen.getByLabelText("Message the agent");
    composer.focus();
    expect(fireEvent.keyDown(composer, { key: "Enter" })).toBe(false);
    expect(progress()).toBe("Question 2 of 2:");
  });

  it.each([
    { name: "typed text", text: "draft", hasDraft: false },
    { name: "attachments or mentions", text: "", hasDraft: true },
  ])("leaves the composer alone when it holds $name", ({ text, hasDraft }) => {
    const { onReject } = renderForm([framework, snacks], {
      beside: composerCard({ text, hasDraft }),
    });
    const composer = screen.getByLabelText("Message the agent");
    composer.focus();
    expect(fireEvent.keyDown(composer, { key: "Enter" })).toBe(true);
    expect(fireEvent.keyDown(composer, { key: "Escape" })).toBe(true);
    expect(progress()).toBe("Question 1 of 2:");
    expect(onReject).not.toHaveBeenCalled();
  });

  it("leaves Enter to the composer when Mod+Enter sends", () => {
    localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    renderForm([framework, snacks], { beside: composerCard() });
    const composer = screen.getByLabelText("Message the agent");
    composer.focus();
    expect(fireEvent.keyDown(composer, { key: "Enter" })).toBe(true);
    expect(progress()).toBe("Question 1 of 2:");
  });

  it("leaves Enter to the composer while its suggestions are open", () => {
    renderForm([framework, snacks], { beside: composerCard({ suggestionsOpen: true }) });
    const composer = screen.getByLabelText("Message the agent");
    composer.focus();
    expect(fireEvent.keyDown(composer, { key: "Enter" })).toBe(true);
    expect(progress()).toBe("Question 1 of 2:");
  });

  it("leaves Enter and Escape to a side-chat composer", () => {
    const { onReject } = renderForm([framework, snacks], {
      beside: composerCard({ sideChat: true }),
    });
    const composer = screen.getByLabelText("Message the agent");
    composer.focus();
    expect(fireEvent.keyDown(composer, { key: "Enter" })).toBe(true);
    fireEvent.keyDown(composer, { key: "Escape" });
    expect(progress()).toBe("Question 1 of 2:");
    expect(onReject).not.toHaveBeenCalled();
  });

  it("never takes Enter from a button, link, or other input", () => {
    renderForm([framework, snacks], {
      beside: (
        <>
          <button type="button">Retry</button>
          <a href="#docs">Docs</a>
          <input aria-label="Search" />
        </>
      ),
    });
    const controls = [
      screen.getByRole("button", { name: "Retry" }),
      screen.getByRole("link", { name: "Docs" }),
      screen.getByLabelText("Search"),
    ];
    for (const control of controls) {
      control.focus();
      expect(fireEvent.keyDown(control, { key: "Enter" })).toBe(true);
    }
    expect(progress()).toBe("Question 1 of 2:");
  });
});

describe("AskUserQuestionForm — keyboard activity", () => {
  it("does not take focus when it appears", () => {
    renderForm([framework, snacks]);
    expect(document.activeElement).toBe(document.body);
  });

  it("keeps focus in the form after a keystroke moves to another question", () => {
    renderForm([framework, snacks]);
    const custom = screen.getByTestId("ask-user-question-custom-input");
    custom.focus();
    fireEvent.keyDown(custom, { key: "Enter" });
    expect(progress()).toBe("Question 2 of 2:");
    expect(document.activeElement).toBe(screen.getByTestId("ask-user-question-section"));
  });

  it("ignores keys pressed outside a form that is not keyboard-active", () => {
    const { onReject } = renderForm([framework, snacks], { keyboardActive: false });
    pressOnPage({ key: "Enter" });
    pressOnPage({ key: "2" });
    pressOnPage({ key: "Escape" });
    expect(progress()).toBe("Question 1 of 2:");
    expect(screen.getByLabelText("Vue")).not.toBeChecked();
    expect(onReject).not.toHaveBeenCalled();
  });

  it("still answers keys pressed inside a form that is not keyboard-active", () => {
    const { onReject } = renderForm([framework, snacks], { keyboardActive: false });
    const custom = screen.getByTestId("ask-user-question-custom-input");
    custom.focus();
    fireEvent.keyDown(custom, { key: "Escape" });
    expect(onReject).toHaveBeenCalledTimes(1);
  });

  it("leaves keys pressed inside another question form to that form", () => {
    const active = { onSubmit: vi.fn(), onReject: vi.fn() };
    const other = { onSubmit: vi.fn(), onReject: vi.fn() };
    // The keyboard-active form mounts first, so its window listener runs first.
    render(
      <>
        <AskUserQuestionForm
          elicitationId={nextElicitationId()}
          questions={[framework]}
          keyboardActive
          {...active}
        />
        <AskUserQuestionForm
          elicitationId={nextElicitationId()}
          questions={[color]}
          keyboardActive={false}
          {...other}
        />
      </>,
    );
    const [activeForm, otherForm] = screen.getAllByTestId("ask-user-question-form");
    const otherOption = within(otherForm!).getAllByRole("radio")[0]!;
    otherOption.focus();

    fireEvent.keyDown(otherOption, { key: "2" });
    expect(within(otherForm!).getAllByRole("radio")[1]).toBeChecked();
    expect(within(activeForm!).getAllByRole("radio")[1]).not.toBeChecked();

    fireEvent.keyDown(otherOption, { key: "Escape" });
    expect(other.onReject).toHaveBeenCalledTimes(1);
    expect(active.onReject).not.toHaveBeenCalled();
  });

  it("answers only from the keyboard-active card when two cards share the page", () => {
    const askCard = (elicitationId: string, question: ClaudeQuestion, active: boolean) => (
      <ApprovalCard
        key={elicitationId}
        elicitationId={elicitationId}
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview=""
        requestedSchema={{}}
        status="pending"
        response={null}
        askUserQuestion={{ questions: [question] }}
        keyboardActive={active}
      />
    );
    const { container } = render(
      <>
        {askCard("elic_inbox", { ...framework, id: "framework-inbox" }, false)}
        {askCard("elic_chat", { ...framework, id: "framework-chat" }, true)}
      </>,
    );
    pressOnPage({ key: "2" });
    const [inboxCard, chatCard] = Array.from(
      container.querySelectorAll<HTMLElement>('[data-testid="approval-card"]'),
    );
    expect(within(inboxCard!).getByLabelText("Vue")).not.toBeChecked();
    expect(within(chatCard!).getByLabelText("Vue")).toBeChecked();
  });
});

describe("AskUserQuestionForm — key hints", () => {
  it("shows digit, Enter, and Esc hints on desktop", () => {
    renderForm([framework, snacks]);
    // Digit chips for the two options and the custom row, then Next and Cancel.
    expect(keyHints()).toHaveLength(5);
    expect(screen.getByRole("button", { name: "Next" }).textContent).toContain("↵");
    expect(screen.getByRole("button", { name: "Cancel" }).textContent).toContain("Esc");
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
  });

  it("marks Submit with Enter only once every question is answered", () => {
    renderForm([framework, snacks]);
    fireEvent.click(screen.getByLabelText("React"));
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    expect(screen.getByRole("button", { name: "Submit" }).textContent).not.toContain("↵");
    fireEvent.click(screen.getByLabelText("Popcorn"));
    expect(screen.getByRole("button", { name: "Submit" }).textContent).toContain("↵");
  });

  it("drops the Next hint when Enter would submit instead of advancing", () => {
    renderForm([framework, snacks]);
    fireEvent.click(screen.getByLabelText("React"));
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    fireEvent.click(screen.getByLabelText("Popcorn"));
    fireEvent.click(screen.getByTestId("ask-user-question-prev"));
    expect(screen.getByRole("button", { name: "Next" }).textContent).not.toContain("↵");
  });

  it.each([
    { name: "a coarse pointer", matches: (query: string) => query.includes("pointer: coarse") },
    { name: "a narrow viewport", matches: (query: string) => query.includes("max-width") },
  ])("hides hints and keeps Enter as a newline on $name", ({ matches }) => {
    stubMedia(matches);
    const { onReject } = renderForm([framework, snacks]);
    expect(keyHints()).toHaveLength(0);
    expect(pressOnPage({ key: "Enter" })).toBe(true);
    expect(progress()).toBe("Question 1 of 2:");
    // Digits and Escape still work for a hardware keyboard.
    pressOnPage({ key: "2" });
    expect(screen.getByLabelText("Vue")).toBeChecked();
    pressOnPage({ key: "Escape" });
    expect(onReject).toHaveBeenCalledTimes(1);
  });
});
