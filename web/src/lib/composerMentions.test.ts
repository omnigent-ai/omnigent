import { describe, expect, it } from "vitest";

import {
  buildMentionPreamble,
  extractSessionMentions,
  rankSessionMentions,
  sessionMentionMarker,
  SESSION_MENTION_HINT,
  stripSessionMentionMarkers,
  type SessionMention,
} from "@/lib/composerMentions";

const alpha: SessionMention = { id: "s_alpha", title: "Alpha planning", isSubagent: false };
const child: SessionMention = { id: "s_child", title: "auth", isSubagent: true };

describe("buildMentionPreamble with sessions", () => {
  it("puts session markers and one hint line before file markers", () => {
    expect(buildMentionPreamble([{ path: "a.ts", isDir: false }], null, [child, alpha])).toBe(
      '[Session: "auth" id=s_child subagent=yes]\n' +
        '[Session: "Alpha planning" id=s_alpha subagent=no]\n' +
        `${SESSION_MENTION_HINT}\n[Attached: a.ts]\n\n`,
    );
  });

  it("omits the hint when no session is tagged", () => {
    expect(buildMentionPreamble([{ path: "a.ts", isDir: false }], null)).toBe(
      "[Attached: a.ts]\n\n",
    );
  });
});

describe("session marker round trip", () => {
  it("neutralizes title characters that would break the marker", () => {
    const marker = sessionMentionMarker({
      id: "s_1",
      title: 'Fix "CI" [flaky]\nnow',
      isSubagent: false,
    });
    expect(marker).toBe('[Session: "Fix CI flaky now" id=s_1 subagent=no]');
    expect(extractSessionMentions(marker)).toEqual([
      { id: "s_1", title: "Fix CI flaky now", isSubagent: false },
    ]);
  });

  it("extracts every tagged session and strips markers plus hint", () => {
    const text = `${buildMentionPreamble([], null, [child, alpha])}summarize both`;
    expect(extractSessionMentions(text)).toEqual([child, alpha]);
    expect(stripSessionMentionMarkers(text)).toBe("summarize both");
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
