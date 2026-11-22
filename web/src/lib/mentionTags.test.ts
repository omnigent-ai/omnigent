import { describe, expect, it } from "vitest";

import {
  composerInlineMentions,
  sessionTagToken,
  type MentionItem,
  type SessionMention,
  type TaggedMentions,
} from "@/lib/composerMentions";

import {
  claimFileTag,
  claimSessionTag,
  mergeTags,
  NO_TAGS,
  tagsGoneFrom,
  tagsRestoredByHistory,
  tagTokens,
  withoutTags,
} from "./mentionTags";

const readme: MentionItem = { path: "README.md", isDir: false, token: "@README.md" };
const readmeLines: MentionItem = {
  path: "README.md",
  isDir: false,
  lineRange: { start: 3, end: 9 },
  token: "@README.md:3-9",
};
const fixCi: SessionMention = {
  id: "s_fixci01",
  title: "Fix CI",
  isSubagent: false,
  token: "@Fix CI",
};
const tags = (files: MentionItem[], sessions: SessionMention[] = []): TaggedMentions => ({
  files,
  sessions,
});

describe("tag sets", () => {
  it("lists each tag's token, a session without one by its title", () => {
    const untitled: SessionMention = { id: "s_2", title: "Plan", isSubagent: false };
    expect(tagTokens(tags([readme], [fixCi, untitled]))).toEqual([
      "@README.md",
      "@Fix CI",
      "@Plan",
    ]);
    expect(sessionTagToken(untitled)).toBe("@Plan");
  });

  it("removes and merges by token, a merged tag replacing one with the same token", () => {
    const renamed = { ...fixCi, title: "Fix CI again" };
    expect(withoutTags(tags([readme, readmeLines], [fixCi]), tags([readme]))).toEqual(
      tags([readmeLines], [fixCi]),
    );
    expect(mergeTags(tags([readme], [fixCi]), tags([], [renamed]))).toEqual(
      tags([readme], [renamed]),
    );
    expect(mergeTags(NO_TAGS, NO_TAGS)).toEqual(NO_TAGS);
  });
});

describe("a session tagged without a token of its own", () => {
  // Quotes, brackets, a newline and repeated spaces, which the composer's token leaves out.
  const messy: SessionMention = {
    id: "s_messy01",
    title: 'Fix "CI" [flaky]\n  tests',
    isSubagent: false,
  };
  const token = "@Fix CI flaky tests";

  it("stands as the same cleaned token the composer sends", () => {
    expect(sessionTagToken(messy)).toBe(token);
    expect(composerInlineMentions([], [messy], null).map((mention) => mention.token)).toEqual([
      token,
    ]);
    expect(tagTokens(tags([], [messy]))).toEqual([token]);
    expect(sessionTagToken({ ...messy, title: " \n " })).toBe("@Untitled session");
  });

  it("stays tagged while its token is in the draft, and an undo restores it", () => {
    const active = tags([], [messy]);
    expect(tagsGoneFrom(active, [`Ask ${token} now`], [token])).toEqual(NO_TAGS);
    expect(tagsRestoredByHistory(active, "Ask  now", `Ask ${token} now`, [token])).toEqual(active);
  });
});

describe("tagsGoneFrom", () => {
  const owned = ["@README.md", "@README.md (file)", "@Fix CI"];

  it("finds the tags whose token is in no segment, such as the tail or a reply quote", () => {
    const active = tags([readme], [fixCi]);
    expect(tagsGoneFrom(active, ["Read @README.md", "and ask @Fix CI"], owned)).toEqual(NO_TAGS);
    expect(tagsGoneFrom(active, ["Read @README.md", "and ask"], owned)).toEqual(tags([], [fixCi]));
  });

  it("doesn't count a token found only inside a longer owned one", () => {
    const shadowed = { ...readme, token: "@README.md" };
    expect(tagsGoneFrom(tags([shadowed]), ["See @README.md (file) now"], owned)).toEqual(
      tags([shadowed]),
    );
  });
});

describe("tagsRestoredByHistory", () => {
  const released = tags([readme], [fixCi]);
  const owned = tagTokens(released);

  it("restores the tags an undo brought back, and only those", () => {
    expect(tagsRestoredByHistory(released, "Ask  now", "Ask @Fix CI now", owned)).toEqual(
      tags([], [fixCi]),
    );
    expect(tagsRestoredByHistory(released, "Ask", "Ask more", owned)).toEqual(NO_TAGS);
  });

  it("leaves a token the text already had as literal text", () => {
    expect(
      tagsRestoredByHistory(released, "@Fix CI typed by hand", "@Fix CI typed by hand!", owned),
    ).toEqual(NO_TAGS);
  });

  it("restores a tag when an undo brings back one more copy of its token", () => {
    expect(
      tagsRestoredByHistory(released, "@README.md and ", "@README.md and @README.md", owned),
    ).toEqual(tags([readme]));
  });
});

describe("claiming a tag", () => {
  it("reuses the active tag for the same file, else the released one, else makes a new one", () => {
    const active = tags([readme]);
    expect(
      claimFileTag({ path: "README.md", isDir: false }, { active, released: NO_TAGS }, []),
    ).toEqual({ item: readme, source: "active" });
    expect(
      claimFileTag(
        { path: "README.md", isDir: false, lineRange: { start: 3, end: 9 } },
        { active, released: tags([readmeLines]) },
        tagTokens(active),
      ),
    ).toEqual({ item: readmeLines, source: "released" });
    expect(
      claimFileTag({ path: "docs/a.md", isDir: false }, { active, released: NO_TAGS }, []),
    ).toEqual({ item: { path: "docs/a.md", isDir: false, token: "@docs/a.md" }, source: "new" });
  });

  it("gives a new file tag a token no released session tag owns", () => {
    const session: SessionMention = { id: "s_readme", title: "README.md", isSubagent: false };
    const released = tags([], [{ ...session, token: "@README.md" }]);
    const { item } = claimFileTag(
      { path: "README.md", isDir: false },
      { active: NO_TAGS, released },
      tagTokens(released),
    );
    expect(item.token).toBe("@README.md (file)");
  });

  it("takes a session's released tag back with its token, or gives it a free one", () => {
    const released = tags([], [fixCi]);
    expect(claimSessionTag(fixCi, { active: NO_TAGS, released }, tagTokens(released))).toEqual({
      session: fixCi,
      source: "released",
    });
    const sameTitle: SessionMention = { id: "s_other99", title: "Fix CI", isSubagent: false };
    const claimed = claimSessionTag(sameTitle, { active: NO_TAGS, released }, tagTokens(released));
    expect(claimed.source).toBe("new");
    expect(claimed.session.token).not.toBe("@Fix CI");
    expect(claimed.session.token).toMatch(/^@Fix CI/);
  });
});
