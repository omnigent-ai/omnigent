import { describe, expect, it } from "vitest";

import {
  detectMentionAt,
  recallSessionMentions,
  composerInlineMentions,
  expandMentionTokens,
  findMentionTokens,
  mentionTokenAtCaret,
  newFileMentionToken,
  presentMentions,
  rankSessionMentions,
  readableSentText,
  replaceSentMentions,
  SESSION_MENTION_HINT,
  sessionMentionMarker,
  sessionMentionToken,
  withSessionMentionHint,
  type SessionMention,
} from "@/lib/composerMentions";

const alpha: SessionMention = { id: "s_alpha", title: "Alpha planning", isSubagent: false };
const child: SessionMention = { id: "s_child", title: "auth", isSubagent: true };

describe("findMentionTokens", () => {
  it("finds tokens at word boundaries, longest first, in text order", () => {
    const text = "see @a.ts:3-9 and @a.ts, not x@a.ts or @a.tsx";
    expect(findMentionTokens(text, ["@a.ts", "@a.ts:3-9"])).toEqual([
      { start: 4, end: 13, token: "@a.ts:3-9" },
      { start: 18, end: 23, token: "@a.ts" },
    ]);
  });

  it("matches multi-word session tokens", () => {
    expect(findMentionTokens("ask @Alpha planning now", ["@Alpha planning"])).toEqual([
      { start: 4, end: 19, token: "@Alpha planning" },
    ]);
  });
});

describe("mentionTokenAtCaret", () => {
  it("returns the token the caret ends or sits inside", () => {
    const text = "ask @Alpha planning now";
    expect(mentionTokenAtCaret(text, 19, ["@Alpha planning"])?.start).toBe(4);
    expect(mentionTokenAtCaret(text, 10, ["@Alpha planning"])?.start).toBe(4);
    expect(mentionTokenAtCaret(text, 4, ["@Alpha planning"])).toBeNull();
    expect(mentionTokenAtCaret(text, 20, ["@Alpha planning"])).toBeNull();
  });
});

describe("unique tokens across files and sessions", () => {
  const readmeSession: SessionMention = { id: "s_readme1", title: "README.md", isSubagent: false };
  const readmeFile = { path: "README.md", isDir: false };
  const targets = (text: string, mentions: ReturnType<typeof composerInlineMentions>) =>
    [...expandMentionTokens(text, mentions).matchAll(/id=(\w+)|Attached: ([^\]]+)/g)].map(
      (m) => m[1] ?? `file:${m[2]}`,
    );

  it("suffixes the file when a session already owns the plain token", () => {
    const sessionToken = sessionMentionToken(readmeSession, []);
    const fileToken = newFileMentionToken(readmeFile, [sessionToken]);
    expect([sessionToken, fileToken]).toEqual(["@README.md", "@README.md (file)"]);
    const mentions = composerInlineMentions(
      [{ ...readmeFile, token: fileToken }],
      [{ ...readmeSession, token: sessionToken }],
      null,
    );
    expect(targets(`compare ${sessionToken} with ${fileToken}`, mentions)).toEqual([
      "s_readme1",
      "file:README.md",
    ]);
  });

  it("suffixes the session when a file already owns the plain token", () => {
    const fileToken = newFileMentionToken(readmeFile, []);
    const sessionToken = sessionMentionToken(readmeSession, [fileToken]);
    expect([fileToken, sessionToken]).toEqual(["@README.md", "@README.md (s_read)"]);
    const mentions = composerInlineMentions(
      [{ ...readmeFile, token: fileToken }],
      [{ ...readmeSession, token: sessionToken }],
      null,
    );
    expect(targets(`compare ${fileToken} with ${sessionToken}`, mentions)).toEqual([
      "file:README.md",
      "s_readme1",
    ]);
  });

  it("keeps same-title sessions apart even when their ids share a prefix", () => {
    const sessions = ["abcdef111111a", "abcdef222222b", "abcdef111111c"].map((id) => ({
      id,
      title: "Fix",
      isSubagent: false,
    }));
    const taken: string[] = [];
    const tagged = sessions.map((session) => {
      const token = sessionMentionToken(session, taken);
      taken.push(token);
      return { ...session, token };
    });
    expect(taken).toEqual(["@Fix", "@Fix (abcdef)", "@Fix (abcdef111111)"]);
    const mentions = composerInlineMentions([], tagged, null);
    expect(targets(taken.join(" and "), mentions)).toEqual(sessions.map((s) => s.id));
  });
});

describe("expanding composer text", () => {
  const mentions = composerInlineMentions(
    [{ path: "src/a.ts", isDir: false }],
    [
      { ...child, token: "@auth" },
      { ...alpha, token: "@Alpha planning" },
    ],
    "codex-native",
  );

  it("replaces each token with its marker where it sits", () => {
    expect(
      expandMentionTokens("Ask @auth to diff @src/a.ts against @Alpha planning.", mentions),
    ).toBe(
      'Ask [Session: "auth" id=s_child subagent=yes] to diff [Attached file: src/a.ts] ' +
        'against [Session: "Alpha planning" id=s_alpha subagent=no].',
    );
  });

  it("does not count a token found only inside a longer token as present", () => {
    const clashing = [
      { token: "@README.md", marker: "[Session: …]" },
      { token: "@README.md (file)", marker: "[Attached: README.md]" },
    ];
    expect(presentMentions(clashing, ["see @README.md (file)"]).map((m) => m.token)).toEqual([
      "@README.md (file)",
    ]);
  });

  it("drops mentions whose token was deleted from every segment", () => {
    expect(presentMentions(mentions, ["only @auth here"]).map((m) => m.token)).toEqual(["@auth"]);
  });

  it("appends the hint once, only when a session is tagged", () => {
    expect(withSessionMentionHint("go ", true)).toBe(`go\n\n${SESSION_MENTION_HINT}`);
    expect(withSessionMentionHint("go", false)).toBe("go");
    // A restored message that already carries the hint keeps one copy.
    const restored = withSessionMentionHint("go", true);
    expect(withSessionMentionHint(`${restored} and more`, true)).toBe(
      `${restored} and more\n\n${SESSION_MENTION_HINT}`,
    );
    expect(withSessionMentionHint(restored, true)).toBe(restored);
  });
});

describe("reading a sent message", () => {
  const sent =
    `Ask ${sessionMentionMarker(child)} about [Attached: src/a.ts]\n` +
    `[Attached: /tmp/upload.png]\n\nthanks\n\n${SESSION_MENTION_HINT}`;

  it("neutralizes title characters that would break the marker", () => {
    expect(sessionMentionMarker({ id: "s_1", title: 'Fix "CI" [x]\nnow', isSubagent: false })).toBe(
      '[Session: "Fix CI x now" id=s_1 subagent=no]',
    );
  });

  it("turns markers into @names, drops uploads and the hint", () => {
    expect(readableSentText(sent)).toBe("Ask @auth about @src/a.ts\nthanks");
  });

  it("stays linear on malformed markers", () => {
    // Each input took seconds with patterns that rescan after an unclosed "[";
    // the test's default timeout is the guard, not a timing assertion.
    const inputs = [
      `${" ".repeat(200_000)}x`,
      "[Attached:".repeat(50_000),
      `[Attached:${" ".repeat(200_000)}`,
      `${"[Tagged sessions:".repeat(20_000)}x`,
      `[Session: "${"a".repeat(200_000)}`,
    ];
    for (const input of inputs) expect(readableSentText(input)).toBe(input);
  });

  it("keeps a session marker the user typed as text when there is no hint", () => {
    const typed = 'why is [Session: "x" id=y subagent=no] inline?';
    expect(readableSentText(typed)).toBe(typed);
    const seen: string[] = [];
    replaceSentMentions(typed, (m) => {
      seen.push(m.kind);
      return "";
    });
    expect(seen).toEqual([]);
  });

  it("keeps hint-looking text the user typed mid-message", () => {
    const typed = "why does [Tagged sessions: read] show up?";
    expect(readableSentText(typed)).toBe(typed);
  });

  it("hands each mention to the renderer", () => {
    const seen: string[] = [];
    replaceSentMentions(sent, (m) => {
      seen.push(m.kind === "session" ? `${m.id}:${m.isSubagent}` : m.path);
      return "";
    });
    expect(seen).toEqual(["s_child:true", "src/a.ts"]);
  });
});

describe("rankSessionMentions", () => {
  it("filters by title case-insensitively, keeps order, dedupes and caps", () => {
    const beta: SessionMention = { id: "s_beta", title: "Beta ALPHA", isSubagent: false };
    expect(rankSessionMentions([alpha, child, beta, alpha], "alpha")).toEqual([alpha, beta]);
    expect(rankSessionMentions([alpha, child, beta], "", 2)).toEqual([alpha, child]);
  });

  it("matches nothing for a path-shaped query", () => {
    expect(rankSessionMentions([alpha], "src/")).toEqual([]);
  });
});

describe("detectMentionAt", () => {
  it("continues across a space only while a listed session title still matches", () => {
    const titles = ["Casual greeting", "Explain Omnigent Repo"];
    expect(detectMentionAt("hi @casual g", 12, titles)).toEqual({
      query: "casual g",
      start: 3,
      end: 12,
    });
    expect(detectMentionAt("@casual ", 8, titles)?.query).toBe("casual ");
    // No listed title contains the query, so the space finishes the token.
    expect(detectMentionAt("@casual x", 9, titles)).toBeNull();
    // A double space or a newline always finishes it.
    expect(detectMentionAt("@casual  ", 9, titles)).toBeNull();
    expect(detectMentionAt("@casual\ng", 9, titles)).toBeNull();
  });
});

describe("recallSessionMentions", () => {
  const plan = (id: string) => `[Session: "Plan" id=${id} subagent=no]`;

  it("turns tagged markers back into unique tokens and drops the hint", () => {
    const recalled = recallSessionMentions(
      `compare ${plan("s_aaaaaaaa")} with ${plan("s_bbbbbbbb")} and ${plan("s_aaaaaaaa")}\n\n` +
        SESSION_MENTION_HINT,
      [`see ${plan("s_bbbbbbbb")}`],
    );
    expect(recalled.sessions.map((s) => [s.id, s.token])).toEqual([
      ["s_aaaaaaaa", "@Plan"],
      ["s_bbbbbbbb", "@Plan (s_bbbb)"],
    ]);
    expect(recalled.main).toBe("compare @Plan with @Plan (s_bbbb) and @Plan");
    expect(recalled.others).toEqual(["see @Plan (s_bbbb)"]);
  });

  it("leaves markers the user typed alone when there is no trailing hint", () => {
    const text = `why does ${plan("s_x")} look like this?`;
    expect(recallSessionMentions(text)).toEqual({ main: text, others: [], sessions: [] });
  });
});
