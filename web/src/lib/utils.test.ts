import { describe, expect, it } from "vitest";

import { cn } from "./utils";

const CUSTOM_TEXT_SIZES = ["text-nano", "text-micro", "text-3xs", "text-2xs", "text-13", "text-15"];

describe("cn custom font-size tokens", () => {
  it.each(CUSTOM_TEXT_SIZES)("preserves %s beside a text color", (fontSize) => {
    expect(cn("text-sm text-white", fontSize)).toBe(`text-white ${fontSize}`);
  });

  it.each(CUSTOM_TEXT_SIZES)("lets a later standard size override %s", (fontSize) => {
    expect(cn(`${fontSize} text-white`, "text-sm")).toBe("text-white text-sm");
  });
});
