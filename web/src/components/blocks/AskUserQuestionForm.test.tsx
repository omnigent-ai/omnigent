// Keyboard driving for AskUserQuestion forms: Enter advances or submits, digits
// pick options, Escape cancels. Keys pressed outside a form reach it only when it
// is keyboardActive, and never while a text field, composer menu, dialog, or other
// focused control owns them.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ReactNode, useState } from "react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as UseSidebarDataModule from "@/hooks/useSidebarData";
import * as CodeMirrorModule from "@/components/composer/engine/CodeMirrorComposerInput";
import { resetCodeMirrorComposerLoadForTests } from "@/components/composer/engine/codeMirrorComposerLoader";
import { ConversationScopeContext } from "@/components/chat/conversationScope";
import { composerTargetOf } from "@/components/composer/engine/composerInput";
import { undo } from "@codemirror/commands";
import { EditorView } from "@codemirror/view";
import { askUserQuestionSignature, getAskUserQuestionDraft } from "@/lib/askUserQuestionDrafts";
import type { ClaudeQuestion } from "@/lib/askUserQuestion";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { SESSION_MENTION_HINT } from "@/lib/composerMentions";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "@/lib/composerSendShortcutPreferences";
import { insertComposerText } from "@/lib/composerText";
import { EmbeddedProvider } from "@/lib/embedded";
import { setEmbedRoot } from "@/lib/host";
import { ApprovalCard } from "./ApprovalCard";
import { AskUserQuestionForm } from "./AskUserQuestionForm";

// Session rows the "@" menu lists from the sidebar; server title search is off.
const sessionList = vi.hoisted(() => ({ loaded: [] as unknown[] }));
vi.mock("@/hooks/useSidebarData", async (importOriginal) => {
  // Like the app's context, the same value until the loaded list changes.
  let cached: { list: unknown[]; value: object } | null = null;
  return {
    ...(await importOriginal<typeof UseSidebarDataModule>()),
    useOptionalLoadedConversations: () => {
      if (cached?.list !== sessionList.loaded) {
        cached = {
          list: sessionList.loaded,
          value: { pages: [{ data: sessionList.loaded }], pageParams: [undefined] },
        };
      }
      return cached.value;
    },
  };
});
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useSessionSearch: () => ({ data: undefined }),
}));

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
  options: { keyboardActive?: boolean; beside?: ReactNode; codeMirror?: boolean } = {},
) {
  const onSubmit = vi.fn();
  const onReject = vi.fn();
  const form = (
    <AskUserQuestionForm
      elicitationId={nextElicitationId()}
      questions={questions}
      onSubmit={onSubmit}
      onReject={onReject}
      keyboardActive={options.keyboardActive ?? true}
    />
  );
  render(
    <>
      {options.codeMirror ? (
        <CapabilitiesProvider info={codeMirrorInfo}>{form}</CapabilitiesProvider>
      ) : (
        form
      )}
      {options.beside}
    </>,
  );
  return { onSubmit, onReject };
}

const codeMirrorInfo = { ...FALLBACK_SERVER_INFO, features: { chat_composer_codemirror: true } };

function sessionRow(id: string, title: string) {
  return { id, object: "conversation", title, created_at: 0, updated_at: 0, labels: {} };
}

/** The custom answer's prompt editor, once its chunk has loaded. */
async function promptEditor() {
  await waitFor(() =>
    expect(document.querySelector('[data-composer-engine="codemirror"]')).not.toBeNull(),
  );
  return screen.getByTestId("ask-user-question-custom-input");
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
        data-composer-input=""
        defaultValue={text}
        data-has-draft={hasDraft ? "true" : undefined}
      />
    </div>
  );
}

/** A native edit of a textarea: the value is set, then the input event carries its type. */
function nativeInput(field: HTMLTextAreaElement, value: string, inputType: string) {
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(
      field,
      value,
    );
    field.dispatchEvent(new InputEvent("input", { bubbles: true, inputType }));
  });
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

  it("leaves Escape to an open side panel, but not to one in a collapsed rail", () => {
    const panel = renderForm([framework], { beside: <div data-escape-owner="">Files</div> });
    expect(pressOnPage({ key: "Escape" })).toBe(true);
    expect(panel.onReject).not.toHaveBeenCalled();
    cleanup();

    // A collapsed workspace rail keeps its file viewer mounted, inert.
    const collapsed = renderForm([framework], {
      beside: (
        // React 18 drops a boolean `inert` prop, so set it as the app does.
        <div ref={(element) => element?.setAttribute("inert", "")}>
          <div data-escape-owner="">Files</div>
        </div>
      ),
    });
    pressOnPage({ key: "Escape" });
    expect(collapsed.onReject).toHaveBeenCalledTimes(1);
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

  it("leaves Escape to a popover that opened without taking focus", async () => {
    function HoverPopover() {
      const [open, setOpen] = useState(true);
      return (
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger>Agent tools and policies</PopoverTrigger>
          <PopoverContent onOpenAutoFocus={(event) => event.preventDefault()}>Tools</PopoverContent>
        </Popover>
      );
    }
    const { onReject } = renderForm([framework], { beside: <HoverPopover /> });
    expect(await screen.findByText("Tools")).toBeInTheDocument();
    const custom = screen.getByTestId("ask-user-question-custom-input");
    custom.focus();
    fireEvent.keyDown(custom, { key: "Escape" });
    await waitFor(() => expect(screen.queryByText("Tools")).toBeNull());
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

describe("AskUserQuestionForm — unseen questions", () => {
  it("shows a later question before Enter submits its default answer", () => {
    const { onSubmit } = renderForm([framework, color]);
    expect(progress()).toBe("Question 1 of 2:");
    pressOnPage({ key: "Enter" });
    expect(progress()).toBe("Question 2 of 2:");
    expect(onSubmit).not.toHaveBeenCalled();
    pressOnPage({ key: "Enter" });
    expect(onSubmit).toHaveBeenCalledOnce();
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

  it("arrows through the custom radio without moving focus into its input", async () => {
    const user = userEvent.setup();
    renderForm([framework]);
    await user.click(screen.getByLabelText("Vue"));
    await user.keyboard("{ArrowDown}");
    const toggle = screen.getByTestId("ask-user-question-custom-toggle");
    expect(toggle).toBeChecked();
    expect(toggle).toHaveFocus();
    // Held down, so no keyup follows, as when the window loses focus mid-press.
    await user.keyboard("{ArrowDown>}");
    expect(screen.getByLabelText("React")).toBeChecked();
    expect(screen.getByLabelText("React")).toHaveFocus();
    await user.click(toggle);
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveFocus();
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

describe("AskUserQuestionForm — disabled Submit", () => {
  it("names the unanswered question that keeps Submit disabled", async () => {
    renderForm([{ ...snacks, header: "Snacks" }, framework]);
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    expect(screen.getByRole("button", { name: "Submit" })).toBeDisabled();
    act(() => screen.getByRole("group", { name: "Submit" }).focus());
    await waitFor(() =>
      expect(screen.getByRole("tooltip")).toHaveTextContent("Answer question 1 (Snacks) to submit"),
    );

    fireEvent.click(screen.getByTestId("ask-user-question-prev"));
    fireEvent.click(screen.getByLabelText("Popcorn"));
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    expect(screen.getByRole("button", { name: "Submit" })).toBeEnabled();
    expect(screen.queryByRole("group", { name: "Submit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("closes the reason on Escape before Escape cancels the questions", async () => {
    const user = userEvent.setup();
    const { onReject } = renderForm([snacks, framework]);
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    act(() => screen.getByRole("group", { name: "Submit" }).focus());
    await waitFor(() => expect(screen.getByRole("tooltip")).toBeVisible());

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(onReject).not.toHaveBeenCalled();

    await user.keyboard("{Escape}");
    expect(onReject).toHaveBeenCalledTimes(1);
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

  it("hides hints and keeps Enter as a newline on a narrow viewport", () => {
    stubMedia((query: string) => query.includes("max-width"));
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

describe("AskUserQuestionForm — prompt editor", () => {
  beforeEach(() => resetCodeMirrorComposerLoadForTests(async () => CodeMirrorModule));
  afterEach(() => {
    resetCodeMirrorComposerLoadForTests();
    sessionList.loaded = [];
  });

  it("answers in the prompt editor when the flag is on", async () => {
    renderForm([framework], { codeMirror: true });
    expect(await promptEditor()).toHaveClass("cm-content");
  });

  it("keeps the textarea when the flag is off", () => {
    renderForm([framework]);
    expect(screen.getByTestId("ask-user-question-custom-input").tagName).toBe("TEXTAREA");
    expect(document.querySelector("[data-composer-engine]")).toBeNull();
  });

  it("submits a typed answer from the editor", async () => {
    const { onSubmit } = renderForm([framework], { codeMirror: true });
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Ramen"));
    // Typing checks the custom row, so the typed answer is the one sent.
    expect(screen.getByTestId("ask-user-question-custom-toggle")).toBeChecked();
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith({ framework: "Ramen" });
  });

  it("advances on Enter from the editor while answers are missing", async () => {
    renderForm([framework, snacks], { codeMirror: true });
    fireEvent.keyDown(await promptEditor(), { key: "Enter" });
    expect(progress()).toBe("Question 2 of 2:");
    // Focus follows the question, as it does from the textarea.
    expect(screen.getByTestId("ask-user-question-section")).toHaveFocus();
  });

  it("leaves Shift+Enter in the editor to the editor, without advancing", async () => {
    renderForm([framework, snacks], { codeMirror: true });
    fireEvent.keyDown(await promptEditor(), { key: "Enter", shiftKey: true });
    expect(progress()).toBe("Question 1 of 2:");
  });

  it("keeps the textarea on a narrow viewport even with the flag on", () => {
    stubMedia((query: string) => query.includes("max-width"));
    renderForm([framework], { codeMirror: true });
    expect(screen.getByTestId("ask-user-question-custom-input").tagName).toBe("TEXTAREA");
    expect(document.querySelector("[data-composer-engine]")).toBeNull();
  });

  it("leaves digits typed in the editor to the editor", async () => {
    renderForm([framework], { codeMirror: true });
    fireEvent.keyDown(await promptEditor(), { key: "2" });
    expect(screen.getByLabelText("Vue")).not.toBeChecked();
  });

  it("tags a session with @ and sends its marker and the tool hint", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const { onSubmit } = renderForm([framework], { codeMirror: true });
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Use @Fix"));
    expect(screen.getByTitle("Tag session Fix CI")).toBeInTheDocument();
    // Enter attaches the highlighted row; the next Enter sends the answer.
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(composerTargetOf(editor)!.value).toBe("Use @Fix CI ");
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      framework: `Use [Session: "Fix CI" id=s_ci subagent=no]\n\n${SESSION_MENTION_HINT}`,
    });
  });

  it("closes the @ menu on Escape before Escape cancels the form", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const { onReject } = renderForm([framework], { codeMirror: true });
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "@Fi"));
    expect(screen.getByTestId("file-mention-menu")).toBeInTheDocument();
    fireEvent.keyDown(editor, { key: "Escape" });
    expect(screen.queryByTestId("file-mention-menu")).toBeNull();
    expect(onReject).not.toHaveBeenCalled();
    fireEvent.keyDown(editor, { key: "Escape" });
    expect(onReject).toHaveBeenCalledExactlyOnceWith();
  });

  const sentAnswer = (title: string, id: string) =>
    `Base it on the fix from [Session: "${title}" id=${id} subagent=no]\n\n${SESSION_MENTION_HINT}`;

  it("shows a submitted answer's session as a pill, without the marker or hint", async () => {
    sessionList.loaded = [sessionRow("de02c3f0", "Fix flaky CI job")];
    const { container } = render(
      <ApprovalCard
        elicitationId="elic_persisted"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview=""
        requestedSchema={{}}
        status="responded"
        response={{
          action: "accept",
          content: { "Which framework?": sentAnswer("Fix flaky CI job", "de02c3f0") },
        }}
        askUserQuestion={{ questions: [framework] }}
      />,
    );
    expect(screen.getByTestId("mention-pill")).toHaveTextContent("@Fix flaky CI job");
    expect(container.textContent).toContain("Base it on the fix from");
    expect(container.textContent).not.toContain("[Session:");
    expect(container.textContent).not.toContain("Tagged sessions");
  });

  it("shows the session pill once the card is answered from the editor", async () => {
    sessionList.loaded = [sessionRow("de02c3f0", "Fix flaky CI job")];
    const onSubmit = vi.fn();
    const card = (response: { action: "accept"; content: Record<string, unknown> } | null) => (
      <CapabilitiesProvider info={codeMirrorInfo}>
        <ApprovalCard
          elicitationId="elic_live"
          message="Claude wants to call AskUserQuestion"
          phase="pre_tool_use"
          policyName="claude_native_permission"
          contentPreview=""
          requestedSchema={{}}
          status={response ? "responded" : "pending"}
          response={response}
          askUserQuestion={{ questions: [framework] }}
          onSubmit={onSubmit}
        />
      </CapabilitiesProvider>
    );
    const { rerender } = render(card(null));
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Base it on the fix from @Fix"));
    fireEvent.keyDown(editor, { key: "Enter" });
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const [, , content] = onSubmit.mock.calls[0]!;
    // The card's answered state, as the store rebuilds it from the submitted verdict.
    rerender(card({ action: "accept", content }));
    expect(screen.getByTestId("mention-pill")).toHaveTextContent("@Fix flaky CI job");
    expect(document.body.textContent).not.toContain("[Session:");
    expect(document.body.textContent).not.toContain("Tagged sessions");
  });

  /** Tags Fix CI in the editor, then deletes its token whole with Backspace. */
  async function tagThenDeleteToken() {
    const onSubmit = vi.fn();
    const elicitationId = nextElicitationId();
    render(
      <CapabilitiesProvider info={codeMirrorInfo}>
        <AskUserQuestionForm
          elicitationId={elicitationId}
          questions={[framework]}
          onSubmit={onSubmit}
          onReject={vi.fn()}
        />
      </CapabilitiesProvider>,
    );
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Use @Fix"));
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(composerTargetOf(editor)!.value).toBe("Use @Fix CI ");
    act(() => composerTargetOf(editor)!.setSelectionRange(11, 11));
    fireEvent.keyDown(editor, { key: "Backspace" });
    expect(composerTargetOf(editor)!.value).toBe("Use  ");
    return { editor, onSubmit, elicitationId };
  }

  it("sends a deleted session token typed again as plain text", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const { editor, onSubmit } = await tagThenDeleteToken();
    act(() => insertComposerText(composerTargetOf(editor)!, "@Fix CI", 5, 5));
    const typed = composerTargetOf(editor)!.value;
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({ framework: typed });
    expect(typed).toBe("Use  @Fix CI");
  });

  it("restores a deleted session token with undo", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const { editor, onSubmit } = await tagThenDeleteToken();
    act(() => void undo(EditorView.findFromDOM(editor)!));
    expect(composerTargetOf(editor)!.value).toBe("Use @Fix CI ");
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      framework: `Use [Session: "Fix CI" id=s_ci subagent=no]\n\n${SESSION_MENTION_HINT}`,
    });
  });

  it("keeps a session tagged in a secret answer out of the saved draft", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const secret: ClaudeQuestion = { ...color, id: "secret", isSecret: true };
    const elicitationId = nextElicitationId();
    render(
      <CapabilitiesProvider info={codeMirrorInfo}>
        <AskUserQuestionForm
          elicitationId={elicitationId}
          questions={[framework, secret]}
          onSubmit={vi.fn()}
          onReject={vi.fn()}
        />
      </CapabilitiesProvider>,
    );
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Use @Fix"));
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(composerTargetOf(editor)!.value).toBe("Use @Fix CI ");
    const draft = getAskUserQuestionDraft(
      elicitationId,
      askUserQuestionSignature([framework, secret]),
    );
    expect(draft?.currentIndex).toBe(1);
    expect(draft?.sessionMentions).toEqual({});
  });

  it("keeps the textarea until an IME composition ends", async () => {
    let finishLoad = () => {};
    const loaded = new Promise<typeof CodeMirrorModule>((resolve) => {
      finishLoad = () => resolve(CodeMirrorModule);
    });
    resetCodeMirrorComposerLoadForTests(() => loaded);
    renderForm([framework], { codeMirror: true });
    const textarea = screen.getByTestId("ask-user-question-custom-input");
    fireEvent.compositionStart(textarea);

    await act(async () => {
      finishLoad();
      await loaded;
    });
    expect(textarea.tagName).toBe("TEXTAREA");
    expect(document.querySelector('[data-composer-engine="codemirror"]')).toBeNull();

    fireEvent.compositionEnd(textarea);
    await promptEditor();
  });

  it("keeps the caret and focus when the editor replaces the textarea", async () => {
    let finishLoad = () => {};
    const loaded = new Promise<typeof CodeMirrorModule>((resolve) => {
      finishLoad = () => resolve(CodeMirrorModule);
    });
    resetCodeMirrorComposerLoadForTests(() => loaded);
    renderForm([framework], { codeMirror: true });
    const textarea = screen.getByTestId("ask-user-question-custom-input") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "Ramen soup" } });
    textarea.focus();
    textarea.setSelectionRange(0, 5);
    fireEvent.select(textarea);

    act(() => finishLoad());
    const editor = await promptEditor();
    expect(document.activeElement).toBe(editor);
    const target = composerTargetOf(editor)!;
    expect([target.selectionStart, target.selectionEnd]).toEqual([0, 5]);
    expect(target.value).toBe("Ramen soup");
  });

  it("keeps a tagged session through a narrow viewport and back", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    // A live matchMedia: narrowing notifies the mobile query's listeners, as a resize does.
    let narrow = false;
    const listeners = new Set<() => void>();
    const matchMedia = window.matchMedia;
    vi.spyOn(window, "matchMedia").mockImplementation((query) => {
      const real = matchMedia(query);
      const mobile = query.includes("max-width");
      return {
        ...real,
        matches: mobile ? narrow : real.matches,
        addEventListener: (type: string, listener: () => void) =>
          mobile ? listeners.add(listener) : real.addEventListener(type, listener),
        removeEventListener: (type: string, listener: () => void) =>
          mobile ? listeners.delete(listener) : real.removeEventListener(type, listener),
      } as MediaQueryList;
    });
    const resize = (nowNarrow: boolean) => {
      narrow = nowNarrow;
      act(() => listeners.forEach((listener) => listener()));
    };
    const { onSubmit } = renderForm([framework], { codeMirror: true });
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Use @Fix"));
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(composerTargetOf(editor)!.value).toBe("Use @Fix CI ");

    resize(true);
    const textarea = await waitFor(() => {
      const field = screen.getByTestId("ask-user-question-custom-input");
      expect(field.tagName).toBe("TEXTAREA");
      return field as HTMLTextAreaElement;
    });
    expect(textarea.value).toBe("Use @Fix CI ");
    // Deleting part of the token releases it; the browser's undo brings it back.
    nativeInput(textarea, "Use @Fix ", "deleteContentBackward");
    nativeInput(textarea, "Use @Fix CI ", "historyUndo");

    resize(false);
    const restored = await promptEditor();
    expect(composerTargetOf(restored)!.value).toBe("Use @Fix CI ");
    fireEvent.keyDown(restored, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      framework: `Use [Session: "Fix CI" id=s_ci subagent=no]\n\n${SESSION_MENTION_HINT}`,
    });
  });

  it("keeps a session tag to the answer it was picked in", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const notes: ClaudeQuestion = {
      id: "notes",
      question: "Any notes?",
      multiSelect: false,
      options: [{ label: "None" }],
    };
    const { onSubmit } = renderForm([framework, notes], { codeMirror: true });
    const first = await promptEditor();
    act(() => insertComposerText(composerTargetOf(first)!, "Use @Fix"));
    fireEvent.keyDown(first, { key: "Enter" });
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    // The second answer types the same words as plain text, so it stays literal.
    const second = await promptEditor();
    act(() => insertComposerText(composerTargetOf(second)!, "Ship @Fix CI"));
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      framework: `Use [Session: "Fix CI" id=s_ci subagent=no]\n\n${SESSION_MENTION_HINT}`,
      notes: "Ship @Fix CI",
    });
  });

  it("keeps undo within each answer: a restore in one question stays there", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const notes: ClaudeQuestion = {
      id: "notes",
      question: "Any notes?",
      multiSelect: false,
      options: [{ label: "None" }],
    };
    const { onSubmit } = renderForm([framework, notes], { codeMirror: true });
    const first = await promptEditor();
    act(() => insertComposerText(composerTargetOf(first)!, "Use @Fix"));
    fireEvent.keyDown(first, { key: "Enter" });
    act(() => composerTargetOf(first)!.setSelectionRange(11, 11));
    fireEvent.keyDown(first, { key: "Backspace" });
    expect(composerTargetOf(first)!.value).toBe("Use  ");
    // Undo in the same answer brings the tag back, before any navigation.
    act(() => void undo(EditorView.findFromDOM(first)!));
    expect(composerTargetOf(first)!.value).toBe("Use @Fix CI ");

    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    const second = await promptEditor();
    act(() => insertComposerText(composerTargetOf(second)!, "Ship"));
    // Undo in the second answer restores nothing from the first.
    act(() => void undo(EditorView.findFromDOM(second)!));
    expect(composerTargetOf(second)!.value).toBe("");

    fireEvent.click(screen.getByTestId("ask-user-question-prev"));
    expect(composerTargetOf(await promptEditor())!.value).toBe("Use @Fix CI ");
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    fireEvent.click(screen.getByLabelText("None"));
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      framework: `Use [Session: "Fix CI" id=s_ci subagent=no]\n\n${SESSION_MENTION_HINT}`,
      notes: "None",
    });
  });

  it("leaves the card's own session out of the @ menu", async () => {
    sessionList.loaded = [sessionRow("s_here", "This session"), sessionRow("s_ci", "Fix CI")];
    render(
      <CapabilitiesProvider info={codeMirrorInfo}>
        <ConversationScopeContext.Provider value="s_here">
          <AskUserQuestionForm
            elicitationId={nextElicitationId()}
            questions={[framework]}
            onSubmit={vi.fn()}
            onReject={vi.fn()}
          />
        </ConversationScopeContext.Provider>
      </CapabilitiesProvider>,
    );
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "@This"));
    expect(screen.queryByTitle("Tag session This session")).toBeNull();
    act(() => composerTargetOf(editor)!.setSelectionRange(0, 0));
    act(() =>
      insertComposerText(composerTargetOf(editor)!, "", 0, composerTargetOf(editor)!.value.length),
    );
    act(() => insertComposerText(composerTargetOf(editor)!, "@Fix"));
    expect(screen.getByTitle("Tag session Fix CI")).toBeInTheDocument();
  });

  it("keeps a tagged session when the card remounts with its draft", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const elicitationId = nextElicitationId();
    const card = (onSubmit: () => void) => (
      <CapabilitiesProvider info={codeMirrorInfo}>
        <AskUserQuestionForm
          elicitationId={elicitationId}
          questions={[framework]}
          onSubmit={onSubmit}
          onReject={vi.fn()}
        />
      </CapabilitiesProvider>
    );
    const first = render(card(vi.fn()));
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Use @Fix"));
    fireEvent.keyDown(editor, { key: "Enter" });
    first.unmount();

    const onSubmit = vi.fn();
    render(card(onSubmit));
    const restored = await promptEditor();
    expect(composerTargetOf(restored)!.value).toBe("Use @Fix CI ");
    fireEvent.keyDown(restored, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      framework: `Use [Session: "Fix CI" id=s_ci subagent=no]\n\n${SESSION_MENTION_HINT}`,
    });
  });
});
