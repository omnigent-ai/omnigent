import { act, renderHook } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearSessionDrafts,
  getSessionDraft,
  setSessionDraft,
  setSessionMentions,
} from "@/lib/sessionDrafts";
import type { TaggedMentions } from "@/lib/composerMentions";
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

describe("useMentionBrowser without an editable input", () => {
  it.each<[string, () => HTMLTextAreaElement | null]>([
    ["missing", () => null],
    ["read-only", () => Object.assign(document.createElement("textarea"), { readOnly: true })],
    ["disabled", () => Object.assign(document.createElement("textarea"), { disabled: true })],
  ])("splices a pick into the draft when the input is %s", (_state, createInput) => {
    const setText = vi.fn();
    const { result } = renderHook(() =>
      useMentionBrowser({
        mention: { query: "rea", start: 6, end: 10 },
        setMention: () => {},
        mentionEntries: [],
        text: "hello @rea world",
        setText,
        textareaRef: { current: createInput() },
      }),
    );

    act(() => result.current.attachMention("readme.md", false));

    expect(setText).toHaveBeenCalledWith("hello @readme.md world");
  });
});

describe("useMentionBrowser when an undo returns to the typed name", () => {
  it.each<[string, string, boolean, string[]]>([
    ["untags the pick the undo reverted", "hello @readme.md", true, []],
    ["keeps the tag when typing makes the same text", "hello @readme.md", false, ["readme.md"]],
    ["keeps the tag of a pick that changed no text", "hello @readme.md world", true, ["readme.md"]],
  ])("%s", (_case, draft, fromHistory, tagged) => {
    const { result } = renderHook(() =>
      useMentionBrowser({
        mention: { query: "readme.md", start: 6, end: 16 },
        setMention: () => {},
        mentionEntries: [],
        text: draft,
        setText: () => {},
        textareaRef: { current: null },
      }),
    );
    act(() => result.current.attachMention("readme.md", false));
    const input = Object.assign(document.createElement("textarea"), { value: draft });

    act(() => result.current.untagUndonePicks({ currentTarget: input, fromHistory }));

    expect(result.current.mentionedItems.map((item) => item.path)).toEqual(tagged);
  });
});
