import { useState, type ComponentProps } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { waitFor, within } from "storybook/test";
import { ComposerAttachments } from "@/components/ComposerAttachments";
import { ChatComposer, ComposerSendButton } from "./ChatComposer";
import { ReplyDraftBlocks } from "./ReplyDraftBlocks";

const KEYBOARD = { submitWithModEnter: false, preventsKeyboardSubmit: false } as const;

const file = (name: string, content = "x", type = "text/plain") =>
  new File([content], name, { type });

const meta = {
  title: "Components/Composer/ChatComposer",
  component: ChatComposer,
  tags: ["visual-snapshot"],
  args: {
    keyboard: KEYBOARD,
    input: { "aria-label": "Message" as const },
  },
  decorators: [
    (Story) => (
      <div className="w-[680px] rounded-2xl bg-muted/30 p-6">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ChatComposer>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {
  args: {
    input: { "aria-label": "Message", placeholder: "Message the agent" },
    actions: {
      leading: <span className="text-ui text-muted-foreground">Context controls</span>,
      trailing: <ComposerSendButton label="Send" />,
    },
  },
};

export const WithDraft: Story = {
  args: {
    input: { "aria-label": "Message", defaultValue: "Summarize the open pull requests" },
    actions: {
      leading: <span className="text-ui text-muted-foreground">Context controls</span>,
      trailing: <ComposerSendButton label="Send" />,
    },
  },
};

export const WithAttachments: Story = {
  args: {
    input: { "aria-label": "Message", placeholder: "Message the agent" },
    slots: {
      attachments: (
        <ComposerAttachments
          files={[file("notes.txt", "hello"), file("src/App.tsx", "code", "text/typescript")]}
          onRemove={() => undefined}
        />
      ),
    },
    actions: {
      leading: <span className="text-ui text-muted-foreground">Context controls</span>,
      trailing: <ComposerSendButton label="Send" />,
    },
  },
};

export const Interrupt: Story = {
  args: {
    input: {
      "aria-label": "Message",
      placeholder: "Queue a follow-up — the agent is working",
    },
    actions: {
      leading: <span className="text-ui text-muted-foreground">Context controls</span>,
      trailing: <ComposerSendButton label="Interrupt" interrupt />,
    },
  },
};

const MARKDOWN_DRAFT = [
  "Please make _this_ italic, **this** bold and `this` inline code.",
  "Leave snake_case_names, file_name.py and \\*escaped\\* markers alone.",
  "~~Drop the old plan~~ and read [the docs](https://example.test/docs) first.",
  "> Quoted, with _slanted **bold** words_ inside.",
  "> A second quoted line joins the same block, long enough to wrap across the width of the composer.",
  "```ts",
  "const total = items.reduce((sum, item) => sum + item.price, 0);",
  "```",
].join("\n");

/** A controlled composer, so the live Markdown highlight layer renders. */
function EditableComposer({
  initialValue,
  ...args
}: ComponentProps<typeof ChatComposer> & { initialValue: string }) {
  const [value, setValue] = useState(initialValue);
  return (
    <ChatComposer
      {...args}
      input={{ ...args.input, value, onChange: (event) => setValue(event.target.value) }}
    />
  );
}

/** Focus the draft with the caret (or selection) at the first match of `text`, plus `offset`. */
function placeCaret(canvasElement: HTMLElement, text: string, offset: number) {
  const textarea = within(canvasElement).getByRole<HTMLTextAreaElement>("textbox");
  const at = textarea.value.indexOf(text) + offset;
  textarea.focus();
  textarea.setSelectionRange(at, at);
}

/**
 * Not being edited, the draft shows as a compact preview: hidden markers take
 * no space, and the code block's fence lines collapse into one tinted box
 * headed by its language.
 */
export const WithMarkdownDraft: Story = {
  args: {
    // Tall enough for the whole draft: Storybook's composer doesn't auto-grow.
    input: { "aria-label": "Message", rows: 11, className: "max-h-none" },
    actions: {
      leading: <span className="text-ui text-muted-foreground">Context controls</span>,
      trailing: <ComposerSendButton label="Send" />,
    },
  },
  render: (args) => <EditableComposer {...args} initialValue={MARKDOWN_DRAFT} />,
  play: async ({ canvasElement }) => {
    await waitFor(() => within(canvasElement).getByTestId("composer-compact-view"));
  },
};

/**
 * While editing, the layout lines up with the textarea: hidden markers keep
 * their width (the gaps), so the caret and selection land on the text.
 */
export const WithMarkdownEditing: Story = {
  ...WithMarkdownDraft,
  play: ({ canvasElement }) => placeCaret(canvasElement, "first.", 6),
};

/** The caret inside a token shows that token's markers, dimmed; the others stay hidden. */
export const WithMarkdownCaretInSpan: Story = {
  ...WithMarkdownDraft,
  play: ({ canvasElement }) => placeCaret(canvasElement, "**this**", 4),
};

/** The caret in a link shows its brackets; its url is always visible, dimmed. */
export const WithMarkdownCaretInLink: Story = {
  ...WithMarkdownDraft,
  play: ({ canvasElement }) => placeCaret(canvasElement, "the docs", 3),
};

/**
 * Consecutive quote lines are one block in the reply quote's look (bar, tint,
 * rounded ends); the caret on a line shows its `>`.
 */
export const WithMarkdownCaretInQuote: Story = {
  ...WithMarkdownDraft,
  play: ({ canvasElement }) => placeCaret(canvasElement, "Quoted", 2),
};

/** A typed `>` quote below a reply quote, whose look it borrows. */
export const WithMarkdownQuoteBesideReply: Story = {
  ...WithMarkdownDraft,
  render: (args) => (
    <EditableComposer
      {...args}
      slots={{
        inputPrefix: (
          <ReplyDraftBlocks
            quotes={[{ id: "q1", before: "", text: "A reply quote, picked from the transcript." }]}
            keyboard={KEYBOARD}
            inputFor={() => ({ "aria-label": "Reply text" })}
            onRemove={() => {}}
            disabled={false}
            activeTextId={null}
          />
        ),
      }}
      initialValue={"> A typed quote, drawn to match it.\n> Its second line joins the block."}
    />
  ),
};

/** The caret anywhere in a fenced block shows its backticks; the info string is always dimmed. */
export const WithMarkdownCaretInCodeBlock: Story = {
  ...WithMarkdownDraft,
  play: ({ canvasElement }) => placeCaret(canvasElement, "const total", 6),
};

/**
 * Typing `**bold` inserts the `**` closer after the caret, so the span is
 * bold while it's typed; one `*` then steps over the closer.
 */
export const WithMarkdownAutoPaired: Story = {
  ...WithMarkdownDraft,
  args: { ...WithMarkdownDraft.args, input: { "aria-label": "Message", rows: 2 } },
  render: (args) => <EditableComposer {...args} initialValue="" />,
  play: async ({ canvasElement }) => {
    const textarea = within(canvasElement).getByRole<HTMLTextAreaElement>("textbox");
    textarea.focus();
    // Native insertText input, as a keystroke sends it (user-event's own value
    // tracking would type over the inserted closer).
    await [..."Now **bold"].reduce(
      (typed, char) =>
        typed.then(() => {
          document.execCommand("insertText", false, char);
          // Each keystroke's own edit lands before the next.
          return new Promise<void>((resolve) => {
            setTimeout(resolve);
          });
        }),
      Promise.resolve(),
    );
  },
};

export const WithMarkdownCommand: Story = {
  args: {
    input: { "aria-label": "Message", accentRange: { start: 0, end: 13 } },
    actions: {
      leading: <span className="text-ui text-muted-foreground">Context controls</span>,
      trailing: <ComposerSendButton label="Send" />,
    },
  },
  render: (args) => (
    <EditableComposer
      {...args}
      initialValue="/cross-review focus on the **cache** path and `useMemo` deps"
    />
  ),
};
