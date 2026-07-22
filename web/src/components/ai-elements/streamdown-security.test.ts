import { describe, expect, it } from "vitest";
import { SYNTAX_THEMES } from "@/lib/syntaxTheme";
import { STREAMDOWN_PLUGINS } from "./streamdown-security";

describe("Streamdown syntax highlighting", () => {
  it("uses the shared Omnigent light and dark syntax themes", () => {
    expect(STREAMDOWN_PLUGINS.code.getThemes()).toEqual(SYNTAX_THEMES);
  });
});
