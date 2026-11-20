import { act, renderHook } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type {
  ComposerInputEvent,
  ComposerKeyboardEvent,
  ComposerTextTarget,
} from "@/components/composer/engine/composerInput";
import {
  createFakeComposerSurface,
  type FakeComposerSurface,
} from "@/components/composer/engine/testing/fakeComposerSurface";
import {
  clearSessionDrafts,
  getSessionDraft,
  setSessionDraft,
  setSessionMentions,
} from "@/lib/sessionDrafts";
import type { MentionState, SessionMention, TaggedMentions } from "@/lib/composerMentions";
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

type Rendered = ReturnType<typeof renderBrowser>["result"];

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
    if (name === "tail") result.current.setText(value);
    else result.current.setQuote(value);
    result.current.browser.noteTextChange(event);
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
    expect(result.current.browser.mentionedItems).toEqual([
      { path: "readme.md", isDir: false, token: "@readme.md" },
    ]);
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

  it("keeps a token pasted over a stale baseline literal through its undo and redo", () => {
    const { result } = renderBrowser();
    tagReadme(result);
    change(result, "Read @readme.md ", false);
    // A reply quote takes the tail's text through controlled state, with no change event.
    act(() => {
      result.current.setQuote("Read @readme.md ");
      result.current.setText("");
    });
    change(result, "Read  ", false, "quote");
    change(result, "@readme.md", false);

    change(result, "", true);
    change(result, "@readme.md", true);
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
    expect(result.current.browser.mentionedSessions).toEqual([
      { id: "sess_fix", title: "Fix CI", isSubagent: false, token: "@Fix CI" },
    ]);
  });

  it("restores a deleted subagent tag when an undo brings its token back", () => {
    const { result } = renderBrowser();
    tagSession(result, reviewSubagent, "Ask @Re");
    change(result, "Ask  ", false);
    expect(result.current.browser.mentionTokens).toEqual([]);

    change(result, "Ask @Review ", true);
    expect(result.current.browser.mentionTokens).toEqual(["@Review"]);
    expect(result.current.browser.mentionedSessions).toEqual([
      { id: "sub_review", title: "Review", isSubagent: true, token: "@Review" },
    ]);
  });

  it("keeps a released file's token reserved, so a new same-token target gets another", () => {
    const { result } = renderBrowser();
    tagReadme(result);
    change(result, "Read  ", false);

    // A session titled "readme.md" naturally takes the released token "@readme.md".
    const readmeSession = { id: "sess_readme", title: "readme.md", isSubagent: false };
    change(result, "Read  @read", false);
    act(() => result.current.setMention({ query: "read", start: 6, end: 11 }));
    act(() => result.current.browser.attachSession(readmeSession));
    change(result, "Read  @readme.md (sess_r) ", false);
    expect(result.current.browser.mentionedItems).toEqual([]);
    expect(result.current.browser.mentionedSessions).toEqual([
      { ...readmeSession, token: "@readme.md (sess_r)" },
    ]);

    // Undo the completion, the typing, then the original deletion, one edit at a time.
    change(result, "Read  @read", true);
    change(result, "Read  ", true);
    change(result, "Read @readme.md ", true);
    expect(result.current.browser.mentionTokens).toEqual(["@readme.md"]);
    expect(result.current.browser.mentionedItems).toEqual([
      { path: "readme.md", isDir: false, token: "@readme.md" },
    ]);
    expect(result.current.browser.mentionedSessions).toEqual([]);
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

describe("useMentionBrowser edits on a live input", () => {
  const mountedSurfaces: FakeComposerSurface[] = [];
  afterEach(() => {
    for (const surface of mountedSurfaces.splice(0)) {
      surface.unregister();
      surface.element.remove();
    }
  });

  function mountSurface(initial: string, options?: { locked?: boolean }) {
    const surface = createFakeComposerSurface(initial, options);
    mountedSurfaces.push(surface);
    return surface;
  }

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
    const input = mountSurface("Read @rea", { locked: true });
    const { result } = renderBrowser(input);
    act(() => result.current.setMention({ query: "rea", start: 5, end: 9 }));
    act(() => result.current.browser.attachMention("readme.md", false));
    expect(input.value).toBe("Read @rea");
    expect(result.current.mention).toEqual({ query: "rea", start: 5, end: 9 });
    expect(result.current.browser.mentionTokens).toEqual([]);
  });

  it("completes a mention as its own undo step, with the caret after the token and its space", () => {
    const input = mountSurface("Read @rea");
    const { result } = renderBrowser(input);
    act(() => result.current.setMention({ query: "rea", start: 5, end: 9 }));
    act(() => result.current.browser.attachMention("readme.md", false));
    expect(input.inserts).toEqual([{ text: "@readme.md ", start: 5, end: 9, separateUndo: true }]);
    expect(input.selectionStart).toBe("Read @readme.md ".length);
  });

  it("deletes the token in the input's current text, even before the draft re-renders", () => {
    const input = mountSurface("Read @rea");
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
  });
});
