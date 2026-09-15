import { describe, expect, it, vi } from "vitest";

import { agentIconUrl, iconLooksPathLike, resolveAgentIcon } from "./agentIcon";

// Two sentinels stand in for the caller's existing harness→glyph mapping:
// ``ICON_KIND_GLYPH`` is what an agent WITH a recognised harness resolves to
// (the "existing iconKind component" case) and ``BOT_FALLBACK`` is the floor an
// unknown harness lands on (the "existing final fallback" case). The resolver
// must delegate to whichever the caller's thunk returns whenever no icon is
// declared — that is the whole point of keeping the precedence in one place.
const ICON_KIND_GLYPH = Symbol("iconKind-glyph");
const BOT_FALLBACK = Symbol("bot-fallback");

describe("iconLooksPathLike", () => {
  // Mirrors omnigent.spec.validator.icon_looks_path_like exactly: a value is
  // path-like when it holds a separator or ends in an image suffix.
  it.each([
    ["brand/icon.svg", true],
    ["nested\\icon.png", true],
    ["logo.svg", true],
    ["logo.PNG", true],
    ["photo.jpeg", true],
    ["art.webp", true],
    ["🦊", false],
    ["robot", false],
    ["👩‍🚀", false],
    ["v1.2", false],
  ])("classifies %s as path-like=%s", (value, expected) => {
    expect(iconLooksPathLike(value)).toBe(expected);
  });
});

describe("agentIconUrl", () => {
  it("points at the read-only per-agent icon endpoint", () => {
    expect(agentIconUrl("ag_1")).toBe("/v1/agents/ag_1/icon");
  });

  it("url-encodes the agent id", () => {
    expect(agentIconUrl("ag/1 2")).toBe("/v1/agents/ag%2F1%202/icon");
  });
});

describe("resolveAgentIcon", () => {
  it("returns the emoji grapheme for a declared emoji icon", () => {
    const res = resolveAgentIcon({ icon: "🦊", id: "ag_1" }, () => ICON_KIND_GLYPH);
    expect(res).toEqual({ kind: "emoji", value: "🦊" });
  });

  it("returns the icon endpoint url for a declared path icon", () => {
    const res = resolveAgentIcon({ icon: "brand/icon.svg", id: "ag_1" }, () => ICON_KIND_GLYPH);
    expect(res).toEqual({ kind: "url", value: "/v1/agents/ag_1/icon" });
  });

  it("falls back to the existing iconKind component when no icon is declared", () => {
    const fallback = vi.fn(() => ICON_KIND_GLYPH);
    const res = resolveAgentIcon({ icon: null, id: "ag_1" }, fallback);
    expect(fallback).toHaveBeenCalledOnce();
    expect(res).toEqual({ kind: "harness", value: ICON_KIND_GLYPH });
  });

  it("falls back to the final harness floor for an unknown harness", () => {
    // Absent icon + a fallback that itself bottoms out at the bot glyph: the
    // resolver must surface exactly what the caller's floor returns.
    const res = resolveAgentIcon({ icon: undefined, id: "ag_1" }, () => BOT_FALLBACK);
    expect(res).toEqual({ kind: "harness", value: BOT_FALLBACK });
  });

  it("treats a whitespace-only icon as absent", () => {
    const res = resolveAgentIcon({ icon: "   ", id: "ag_1" }, () => BOT_FALLBACK);
    expect(res).toEqual({ kind: "harness", value: BOT_FALLBACK });
  });

  it("falls back when a path-like icon has no agent id to build the endpoint", () => {
    // A path icon is only servable via /v1/agents/{id}/icon; with no id there
    // is nothing to point <img> at, so it degrades rather than emitting a
    // broken url.
    const res = resolveAgentIcon({ icon: "brand/icon.svg", id: null }, () => BOT_FALLBACK);
    expect(res).toEqual({ kind: "harness", value: BOT_FALLBACK });
  });
});
