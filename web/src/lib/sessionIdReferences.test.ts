import { describe, expect, it } from "vitest";
import {
  parseSessionIdReferenceTitle,
  remarkSessionIdReferences,
  streamedTextEndsWithId,
} from "./sessionIdReferences";

const ID = "e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9";
const TITLE = `omnigent-session-id:${ID}`;

interface Node {
  type: string;
  value?: string;
  url?: string;
  title?: string;
  children?: Node[];
}

function paragraph(...children: Node[]): Node {
  return { type: "root", children: [{ type: "paragraph", children }] };
}

function marked(tree: Node): Node[] {
  remarkSessionIdReferences()(tree);
  return tree.children![0].children!;
}

function references(text: string) {
  return marked(paragraph({ type: "text", value: text }))
    .filter((node) => node.type === "link")
    .map((node) => parseSessionIdReferenceTitle(node.title));
}

function referencedIds(text: string): string[] {
  return references(text).map((reference) => reference?.id ?? "");
}

describe("remarkSessionIdReferences", () => {
  it("splits a bare id in prose into a reference link between its text", () => {
    expect(marked(paragraph({ type: "text", value: `Open ${ID} and retry.` }))).toEqual([
      { type: "text", value: "Open " },
      { type: "link", url: "#session-id", title: TITLE, children: [{ type: "text", value: ID }] },
      { type: "text", value: " and retry." },
    ]);
  });

  it("wraps inline code that is exactly an id, keeping the code as its text", () => {
    const code = { type: "inlineCode", value: ID };
    expect(marked(paragraph(code, { type: "text", value: " stopped." }))).toEqual([
      { type: "link", url: "#session-id", title: TITLE, children: [code] },
      { type: "text", value: " stopped." },
    ]);
  });

  it.each([
    [`${ID}`, [ID]],
    [`(${ID})`, [ID]],
    [`ids: ${ID}, ${"a".repeat(32)}.`, [ID, "a".repeat(32)]],
    [`**${ID}**`, [ID]],
    [`"${ID}": done`, [ID]],
    [`first ${ID}\nsecond ${"b".repeat(32)}`, [ID, "b".repeat(32)]],
  ])("references the id in %j", (text, ids) => {
    expect(referencedIds(text)).toEqual(ids);
  });

  it.each([
    ["a chat path", `/c/${ID}`],
    ["a deep link", `omnigent://example.test/c/${ID}`],
    ["a legacy prefix", `conv_${ID}`],
    ["a file name", `${ID}.json`],
    ["a longer hash", `${ID}deadbeef`],
    ["a dashed token", `${ID}-1`],
    ["uppercase hex", ID.toUpperCase()],
    ["a short hex id", ID.slice(0, 14)],
    ["a numeric id", "28772569657442"],
    ["a query value", `?id=${ID}`],
    ["a later query value", `?ids=first,${ID}`],
    ["a windows path", `C:\\runs\\${ID}\\out.log`],
    ["an assignment", `session=${ID}`],
    ["a fragment", `#${ID}`],
    ["a handle", `@${ID}`],
    ["a key", `key:${ID}`],
  ])("leaves %s alone", (_label, text) => {
    expect(referencedIds(text)).toEqual([]);
  });

  it("tags only the id that ends the block as trailing", () => {
    const trailing = (text: string) => references(text).map((reference) => reference?.trailing);
    expect(trailing(`Open ${ID}`)).toEqual([true]);
    expect(trailing(`Open ${ID}.`)).toEqual([true]);
    expect(trailing(`Open ${ID} and ${ID}`)).toEqual([false, true]);
    expect(trailing(`Open ${ID} now`)).toEqual([false]);
    const code = { type: "inlineCode", value: ID };
    expect(marked(paragraph({ type: "text", value: "Open " }, code))[1]).toMatchObject({
      title: `${TITLE}:end`,
    });
  });

  it.each([
    [`Open ${ID}`, true],
    [`Open \`${ID}`, true],
    [`Open **${ID}**.`, true],
    [`Open \`${ID}\``, false],
    [`Open ${ID} `, false],
    [`Open ${ID}\n\nThen`, false],
  ])("knows whether streamed %j may still extend the id", (markdown, extendable) => {
    expect(streamedTextEndsWithId(markdown, ID)).toBe(extendable);
  });

  it("leaves link text, partial inline code and fenced code alone", () => {
    const link = {
      type: "link",
      url: "https://example.com",
      children: [{ type: "text", value: ID }],
    };
    const code = { type: "inlineCode", value: `git show ${ID}` };
    const fence = { type: "code", value: ID };
    const tree: Node = {
      type: "root",
      children: [{ type: "paragraph", children: [link, code] }, fence],
    };
    const before = structuredClone(tree);
    remarkSessionIdReferences()(tree);
    expect(tree).toEqual(before);
  });
});

describe("parseSessionIdReferenceTitle", () => {
  it.each([
    [TITLE, { id: ID, trailing: false }],
    [`${TITLE}:end`, { id: ID, trailing: true }],
    [`omnigent-session-id:${ID}x`, null],
    ["omnigent-session-id:../x", null],
    [ID, null],
    [undefined, null],
  ])("parses %j", (title, expected) => {
    expect(parseSessionIdReferenceTitle(title)).toEqual(expected);
  });
});
