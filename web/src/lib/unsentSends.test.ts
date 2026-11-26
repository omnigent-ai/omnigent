import { beforeEach, describe, expect, it } from "vitest";

import {
  clearUnsentSends,
  forgetUnsentSend,
  peekUnsentSends,
  rememberUnsentSend,
} from "./unsentSends";

const KEY = "omnigent.unsentSends";

beforeEach(() => {
  clearUnsentSends();
});

describe("unsent-send records", () => {
  it("reads the single-record shape a previous build wrote", () => {
    window.sessionStorage.setItem(
      KEY,
      JSON.stringify({ conv_1: { stableId: "s1", text: "old text", authorId: "user_a" } }),
    );

    expect(peekUnsentSends("conv_1")).toEqual([
      { stableId: "s1", text: "old text", authorId: "user_a" },
    ]);
  });

  it("keeps one record per message, and forgetting one leaves the others", () => {
    rememberUnsentSend("conv_1", { stableId: "s1", text: "one", authorId: "user_a" });
    rememberUnsentSend("conv_1", { stableId: "s2", text: "two", authorId: "user_a" });

    forgetUnsentSend("conv_1", "s1");

    expect(peekUnsentSends("conv_1").map((r) => r.text)).toEqual(["two"]);
  });

  it("replaces a message's record when it is remembered again", () => {
    rememberUnsentSend("conv_1", { stableId: "s1", text: "one", authorId: "user_a" });
    rememberUnsentSend("conv_1", { stableId: "s1", text: "one, edited", authorId: "user_a" });

    expect(peekUnsentSends("conv_1").map((r) => r.text)).toEqual(["one, edited"]);
  });
});
