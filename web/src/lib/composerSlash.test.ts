import { describe, expect, it } from "vitest";

import { detectSlashTokenAt, spliceSlashToken } from "@/lib/composerSlash";

// These tests pin *where* the "/" menu may open. The composer reads the token
// from the caret, so completion has to work mid-draft — while text that merely
// contains a slash (paths, "and/or", dates) must never trigger it.
describe("detectSlashTokenAt", () => {
  it("detects a bare leading slash", () => {
    expect(detectSlashTokenAt("/", 1)).toEqual({
      query: "",
      start: 0,
      end: 1,
      leading: true,
    });
  });

  it("detects a partially typed leading command", () => {
    expect(detectSlashTokenAt("/des", 4)).toEqual({
      query: "des",
      start: 0,
      end: 4,
      leading: true,
    });
  });

  it("detects a token typed mid-sentence and marks it non-leading", () => {
    const text = "please run /des";
    expect(detectSlashTokenAt(text, text.length)).toEqual({
      query: "des",
      start: 11,
      end: 15,
      leading: false,
    });
  });

  it("detects a token after a newline and marks it non-leading", () => {
    const text = "context first\n/des";
    expect(detectSlashTokenAt(text, text.length)).toEqual({
      query: "des",
      start: 14,
      end: 18,
      leading: false,
    });
  });

  it("treats leading whitespace as still leading", () => {
    // The composer trims for submit routing, so "  /x" invokes just like "/x";
    // the menu must agree or the two surfaces disagree on what's a command.
    expect(detectSlashTokenAt("  /x", 4)?.leading).toBe(true);
  });

  it("closes on the space that ends the token", () => {
    expect(detectSlashTokenAt("/deslop ", 8)).toBeNull();
  });

  it("reads the token at the caret, not at the end of the draft", () => {
    // Caret parked just after "/des" while "hello" trails it: the user is
    // editing the command name, so the menu must reopen on that token.
    const text = "/des hello";
    expect(detectSlashTokenAt(text, 4)).toEqual({
      query: "des",
      start: 0,
      end: 4,
      leading: true,
    });
  });

  it("ignores a slash glued to the previous word", () => {
    expect(detectSlashTokenAt("and/or", 6)).toBeNull();
    expect(detectSlashTokenAt("2026/09", 7)).toBeNull();
  });

  it("stops matching once a path's second slash is typed", () => {
    // "/etc" alone reads as a command shape (and harmlessly yields no
    // matches); the second slash makes it unambiguously a path.
    expect(detectSlashTokenAt("/etc", 4)?.query).toBe("etc");
    expect(detectSlashTokenAt("/etc/hosts", 10)).toBeNull();
    expect(detectSlashTokenAt("see /etc/hosts", 14)).toBeNull();
  });

  it("keeps namespaced plugin skill names in one token", () => {
    expect(detectSlashTokenAt("/dev-productivity:simpl", 23)?.query).toBe("dev-productivity:simpl");
  });
});

describe("spliceSlashToken", () => {
  it("replaces a leading token in place", () => {
    const token = detectSlashTokenAt("/des", 4)!;
    expect(spliceSlashToken("/des", token, "/deslop")).toEqual({
      text: "/deslop ",
      caret: 8,
    });
  });

  it("preserves text on both sides of a mid-draft token", () => {
    const text = "run /des on this file";
    // Caret sits at the end of the token, not the end of the draft.
    const token = detectSlashTokenAt(text, 8)!;
    expect(spliceSlashToken(text, token, "/deslop")).toEqual({
      text: "run /deslop  on this file",
      caret: 12,
    });
  });

  it("leaves a caret positioned for an argument", () => {
    const token = detectSlashTokenAt("/", 1)!;
    const { text, caret } = spliceSlashToken("/", token, "/deep-research");
    expect(text.slice(caret)).toBe("");
    // The trailing space closes the menu — a finished token no longer matches.
    expect(detectSlashTokenAt(text, caret)).toBeNull();
  });
});
