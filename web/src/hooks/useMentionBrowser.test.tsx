import { act, renderHook } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
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

describe("useMentionBrowser while the listing loads", () => {
  function press(key: string, ctrlKey = true) {
    const nativeEvent = new KeyboardEvent("keydown", { key, ctrlKey });
    let prevented = false;
    const event = {
      key,
      code: `Key${key.toUpperCase()}`,
      altKey: false,
      ctrlKey,
      metaKey: false,
      shiftKey: false,
      repeat: false,
      defaultPrevented: false,
      nativeEvent,
      currentTarget: {} as never,
      preventDefault: () => {
        prevented = true;
      },
      stopPropagation: () => {},
    };
    return { event, prevented: () => prevented };
  }

  function renderLoadingBrowser(listingPending: boolean) {
    return renderHook(() =>
      useMentionBrowser({
        mention: { query: "", start: 0, end: 1 },
        setMention: () => {},
        mentionEntries: [],
        text: "@",
        setText: () => {},
        textareaRef: { current: null },
        listingPending,
      }),
    );
  }

  it.each(["n", "p"])("holds Ctrl-%s while only the loading row shows", (letter) => {
    const { result } = renderLoadingBrowser(true);
    expect(result.current.mentionOpen).toBe(false);
    const { event, prevented } = press(letter);
    expect(result.current.handleKeyDown(event)).toBe(true);
    expect(prevented()).toBe(true);
  });

  it("leaves other keys, and Ctrl-N once nothing is loading, to the composer", () => {
    const loading = renderLoadingBrowser(true);
    expect(loading.result.current.handleKeyDown(press("a", false).event)).toBe(false);
    const idle = renderLoadingBrowser(false);
    expect(idle.result.current.handleKeyDown(press("n").event)).toBe(false);
  });
});
