import { act, renderHook } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type {
  ComposerInputEvent,
  ComposerKeyboardEvent,
  ComposerTextTarget,
} from "@/components/composer/engine/composerInput";
import { createFakeComposerSurface } from "@/components/composer/engine/testing/fakeComposerSurface";
import {
  clearSessionDrafts,
  getSessionDraft,
  setSessionDraft,
  setSessionMentions,
} from "@/lib/sessionDrafts";
import {
  presentTaggedMentions,
  type MentionState,
  type SessionMention,
  type TaggedMentions,
} from "@/lib/composerMentions";
import { useMentionBrowser } from "./useMentionBrowser";

const childId = "conv_hook_child";
const readme = { path: "readme.md", isDir: false, token: "@readme.md" };
const fixSession = { id: "sess_fix", title: "Fix CI", isSubagent: false, token: "@Fix CI" };
const otherSession = { id: "sess_other", title: "Other", isSubagent: false, token: "@Other" };
const NO_TAGS: TaggedMentions = { files: [], sessions: [] };

/** The hook with its tags written to the session draft, wired as a grid cell wires them. */
function renderTaggedBrowser() {
  return renderHook(() => {
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const draft = getSessionDraft(childId);
    return useMentionBrowser({
      mention: null,
      setMention: () => {},
      mentionEntries: [],
      text: draft?.text ?? "",
      setText: () => {},
      textareaRef,
      tags: {
        mentions: draft?.mentions ?? NO_TAGS,
        updateMentions: (update) =>
          setSessionMentions(childId, update(getSessionDraft(childId)?.mentions ?? NO_TAGS)),
      },
    });
  });
}

describe("useMentionBrowser with tags held by a session draft", () => {
  beforeEach(() => {
    setSessionDraft(childId, {
      text: "@readme.md @Fix CI ",
      files: [],
      mentions: { files: [readme], sessions: [fixSession] },
    });
  });
  afterEach(() => clearSessionDrafts());

  it("applies two tag writes in one event to the latest draft, not the rendered one", () => {
    const { result } = renderTaggedBrowser();

    act(() => {
      result.current.setMentionedItems([]);
      result.current.setMentionedSessions([]);
    });

    expect(getSessionDraft(childId)?.mentions).toBeUndefined();
  });

  it("gives each functional update the latest tags, so each keeps the other collection", () => {
    const { result } = renderTaggedBrowser();

    act(() => {
      result.current.setMentionedItems((previous) =>
        previous.filter((item) => item.path !== readme.path),
      );
      result.current.setMentionedSessions((previous) => [...previous, otherSession]);
    });

    expect(getSessionDraft(childId)?.mentions).toEqual({
      files: [],
      sessions: [fixSession, otherSession],
    });
  });
});

// Without `input`, edits go through `setText`, as for an input that isn't on screen.
function renderBrowser(input: ComposerTextTarget | null = null) {
  return renderHook(() => {
    const [text, setText] = useState(input?.value ?? "");
    // A reply quote's text, which ChatPage passes alongside the tail as a draft segment.
    const [quote, setQuote] = useState("");
    const [mention, setMention] = useState<MentionState | null>(null);
    const browser = useMentionBrowser({
      mention,
      setMention,
      mentionEntries: [],
      text,
      setText,
      textareaRef: { current: input },
      segments: [text, quote],
    });
    return { text, setText, setQuote, mention, setMention, browser };
  });
}

type Rendered =
  | ReturnType<typeof renderBrowser>["result"]
  | ReturnType<typeof renderCallerOwnedBrowser>["result"];

const reviewSubagent: SessionMention = { id: "sub_review", title: "Review", isSubagent: true };

function tagReadme(result: Rendered) {
  act(() => result.current.setText("Read @rea"));
  act(() => result.current.setMention({ query: "rea", start: 5, end: 9 }));
  act(() => result.current.browser.attachMention("readme.md", false));
}

/** Tag `session` from the "@" query at the end of `typed`. */
function tagSession(result: Rendered, session: SessionMention, typed: string) {
  const start = typed.indexOf("@");
  act(() => result.current.setText(typed));
  act(() => result.current.setMention({ query: typed.slice(start + 1), start, end: typed.length }));
  act(() => result.current.browser.attachSession(session));
}

/**
 * The hook with its tags held by the caller, as a grid cell holds them: the caller drops a tag
 * whose token left the text on every text change, as its shared draft does.
 */
function renderCallerOwnedBrowser() {
  return renderHook(() => {
    const [text, setTextState] = useState("");
    const [mentions, setMentions] = useState<TaggedMentions>(NO_TAGS);
    const [mention, setMention] = useState<MentionState | null>(null);
    const setText = (next: string) => {
      setTextState(next);
      setMentions((previous) => presentTaggedMentions(previous, [next]) ?? NO_TAGS);
    };
    const browser = useMentionBrowser({
      mention,
      setMention,
      mentionEntries: [],
      text,
      setText,
      textareaRef: { current: null },
      tags: { mentions, updateMentions: (update) => setMentions((previous) => update(previous)) },
    });
    return { text, setText, setQuote: () => {}, setMention, browser, mentions };
  });
}

// One stable fake input per name, as a mounted composer input is the same object across events.
const fakeInputs = new WeakMap<Rendered, Map<string, { value: string }>>();

/** A change in input `name` ("tail" or a reply quote), as the composer's onChange reports it. */
function change(result: Rendered, value: string, fromHistory: boolean, name = "tail") {
  const inputs = fakeInputs.get(result) ?? new Map<string, { value: string }>();
  fakeInputs.set(result, inputs);
  const input = inputs.get(name) ?? { value: "" };
  inputs.set(name, input);
  input.value = value;
  const event: ComposerInputEvent = {
    currentTarget: input as unknown as ComposerTextTarget,
    fromHistory,
  };
  act(() => {
    result.current.browser.noteTextChange(event);
    if (name === "tail") result.current.setText(value);
    else result.current.setQuote(value);
  });
}

describe("useMentionBrowser released tags", () => {
  it("restores a deleted file tag when an undo brings its token back", () => {
    const { result } = renderBrowser();
    tagReadme(result);
    change(result, "Read  ", false);
    expect(result.current.browser.mentionTokens).toEqual([]);

    change(result, "Read @readme.md ", true);
    expect(result.current.browser.mentionTokens).toEqual(["@readme.md"]);
  });

  it("keeps a token pasted as an input's first edit literal through its undo and redo", () => {
    const { result } = renderBrowser();
    tagReadme(result);
    change(result, "Read  ", false);
    change(result, "@readme.md", false, "quote");

    change(result, "", true, "quote");
    change(result, "@readme.md", true, "quote");
    expect(result.current.browser.mentionTokens).toEqual([]);
  });

  it("compares a quote's undo with that quote's own text, not the tail's", () => {
    const { result } = renderBrowser();
    tagReadme(result);
    change(result, "Read  ", false);
    // The token retyped by hand in a quote stays literal text.
    change(result, "y @readme.md", false, "quote");
    change(result, "Read  x", false);
    // An undo in the quote that only removes "y " brings no token back.
    change(result, "@readme.md", true, "quote");
    expect(result.current.browser.mentionTokens).toEqual([]);
  });

  it("restores a deleted session tag when an undo brings its token back", () => {
    const { result } = renderBrowser();
    tagSession(result, { id: "sess_fix", title: "Fix CI", isSubagent: false }, "Ask @Fi");
    expect(result.current.browser.mentionTokens).toEqual(["@Fix CI"]);
    change(result, "Ask  ", false);
    expect(result.current.browser.mentionTokens).toEqual([]);

    change(result, "Ask @Fix CI ", true);
    expect(result.current.browser.mentionTokens).toEqual(["@Fix CI"]);
  });

  it("restores a deleted subagent tag when an undo brings its token back", () => {
    const { result } = renderBrowser();
    tagSession(result, reviewSubagent, "Ask @Re");
    change(result, "Ask  ", false);
    expect(result.current.browser.mentionTokens).toEqual([]);

    change(result, "Ask @Review ", true);
    expect(result.current.browser.mentionTokens).toEqual(["@Review"]);
  });

  it("keeps a deleted tag's token as literal text when it is typed again", () => {
    const { result } = renderBrowser();
    tagReadme(result);
    change(result, "Read  ", false);
    change(result, "Read @readme.md ", false);
    expect(result.current.browser.mentionTokens).toEqual([]);
  });

  it("keeps retyped literal text literal when an undo or redo changes something else", () => {
    const { result } = renderBrowser();
    tagReadme(result);
    change(result, "Read  ", false);
    change(result, "Literal @readme.md ", false);
    change(result, "Literal @readme.md x", false);

    change(result, "Literal @readme.md ", true);
    expect(result.current.browser.mentionTokens).toEqual([]);
    change(result, "Literal @readme.md x", true);
    expect(result.current.browser.mentionTokens).toEqual([]);
  });

  it("keeps a retyped token literal when an undo and redo replay that typing", () => {
    const { result } = renderBrowser();
    tagReadme(result);
    change(result, "Read  ", false);
    change(result, "Read @readme.md ", false);

    change(result, "Read  ", true);
    change(result, "Read @readme.md ", true);
    expect(result.current.browser.mentionTokens).toEqual([]);
  });

  it("keeps a retyped token literal when an undo brings back its deletion", () => {
    const { result } = renderBrowser();
    tagReadme(result);
    change(result, "Read  ", false);
    change(result, "Read @readme.md ", false);
    change(result, "Read  ", false);

    change(result, "Read @readme.md ", true);
    expect(result.current.browser.mentionTokens).toEqual([]);
  });

  it("forgets deleted tags on a reset, so an undo in the next draft can't restore them", () => {
    const { result } = renderBrowser();
    tagReadme(result);
    change(result, "Read  ", false);
    act(() => result.current.browser.resetMentions());

    change(result, "Read @readme.md ", true);
    expect(result.current.browser.mentionTokens).toEqual([]);
  });

  it("tags files from outside the menu, reusing a deleted tag's token for the same file", () => {
    const { result } = renderBrowser();
    tagReadme(result);
    change(result, "Read  ", false);

    // The drain appends the tokens to the draft in the same update.
    let tokens: string[] = [];
    act(() => {
      tokens = result.current.browser.tagFiles([
        { path: "readme.md", isDir: false },
        { path: "notes.md", isDir: false },
        { path: "notes.md", isDir: false },
      ]);
      result.current.setText(`Read  ${tokens[0]} ${tokens[1]} `);
    });
    expect(tokens).toEqual(["@readme.md", "@notes.md", "@notes.md"]);
    expect(result.current.browser.mentionTokens).toEqual(["@readme.md", "@notes.md"]);
  });
});

describe("useMentionBrowser with caller-owned tags", () => {
  it("restores a tag the caller dropped on a delete when an undo brings its token back", () => {
    const { result } = renderCallerOwnedBrowser();
    tagReadme(result);
    expect(result.current.browser.mentionTokens).toEqual(["@readme.md"]);

    change(result, "Read  ", false);
    expect(result.current.browser.mentionTokens).toEqual([]);

    change(result, "Read @readme.md ", true);
    expect(result.current.browser.mentionTokens).toEqual(["@readme.md"]);
  });

  it("archives a dropped tag that's only the start of a kept one, so an undo restores both", () => {
    const { result } = renderCallerOwnedBrowser();
    const fix: SessionMention = { id: "sess_fix", title: "Fix", isSubagent: false };
    const fixCi: SessionMention = { id: "sess_fix_ci", title: "Fix CI", isSubagent: false };
    tagSession(result, fix, "Ask @Fi");
    act(() => result.current.setText("Ask @Fix @Fi"));
    act(() => result.current.setMention({ query: "Fi", start: 9, end: 12 }));
    act(() => result.current.browser.attachSession(fixCi));
    expect(result.current.text).toBe("Ask @Fix @Fix CI ");

    change(result, "Ask  @Fix CI ", false);
    expect(result.current.mentions.sessions.map((session) => session.id)).toEqual(["sess_fix_ci"]);

    change(result, "Ask @Fix @Fix CI ", true);
    expect(result.current.mentions.sessions.map((session) => session.id).sort()).toEqual([
      "sess_fix",
      "sess_fix_ci",
    ]);
  });

  it("forgets the tags a reset clears, so an undo in the next draft can't restore them", () => {
    const { result } = renderCallerOwnedBrowser();
    tagReadme(result);
    act(() => {
      result.current.browser.resetMentions();
      result.current.setText("");
    });
    change(result, "", false);

    change(result, "Read @readme.md ", true);
    expect(result.current.browser.mentionTokens).toEqual([]);
  });
});

describe("useMentionBrowser edits on a live input", () => {
  const backspace = (target: ComposerTextTarget): ComposerKeyboardEvent =>
    ({
      key: "Backspace",
      altKey: false,
      metaKey: false,
      ctrlKey: false,
      currentTarget: target,
      preventDefault: () => {},
    }) as unknown as ComposerKeyboardEvent;

  it("leaves the tags and the open menu alone when a locked input refuses the pick", () => {
    const input = createFakeComposerSurface("Read @rea", { locked: true });
    const { result } = renderBrowser(input);
    act(() => result.current.setMention({ query: "rea", start: 5, end: 9 }));
    act(() => result.current.browser.attachMention("readme.md", false));
    expect(input.value).toBe("Read @rea");
    expect(result.current.mention).toEqual({ query: "rea", start: 5, end: 9 });
    expect(result.current.browser.mentionTokens).toEqual([]);
    input.unregister();
  });

  it("completes a mention as its own undo step, with the caret after the token and its space", () => {
    const input = createFakeComposerSurface("Read @rea");
    const { result } = renderBrowser(input);
    act(() => result.current.setMention({ query: "rea", start: 5, end: 9 }));
    act(() => result.current.browser.attachMention("readme.md", false));
    expect(input.inserts).toEqual([{ text: "@readme.md ", start: 5, end: 9, separateUndo: true }]);
    expect(input.selectionStart).toBe("Read @readme.md ".length);
    input.unregister();
  });

  it("deletes the token in the input's current text, even before the draft re-renders", () => {
    const input = createFakeComposerSurface("Read @rea");
    const { result } = renderBrowser(input);
    act(() => result.current.setMention({ query: "rea", start: 5, end: 9 }));
    // The input's change event brings its text into the draft, as the composer's onChange does.
    act(() => {
      result.current.browser.attachMention("readme.md", false);
      result.current.setText(input.value);
    });
    // A Backspace deleted the trailing space, and the draft hasn't caught up yet.
    input.value = "Read @readme.md";
    input.setSelectionRange(15, 15);

    act(() => void result.current.browser.handleTokenBackspace(backspace(input)));
    expect(input.value).toBe("Read ");
    expect(input.inserts.at(-1)).toEqual({ text: "", start: 5, end: 15, separateUndo: true });
    input.unregister();
  });
});
