import { describe, expect, it } from "vitest";
import { normalizeExplicitMathDelimiters } from "./mathMarkdown";

describe("normalizeExplicitMathDelimiters — dollar handling", () => {
  it("converts TeX delimiters to dollars outside code", () => {
    expect(normalizeExplicitMathDelimiters("area is \\(a^2\\)")).toBe("area is $a^2$");
    expect(normalizeExplicitMathDelimiters("\\[a+b\\]")).toBe("$$a+b$$");
  });

  it("escapes currency so prose doesn't render as math", () => {
    expect(normalizeExplicitMathDelimiters("it costs $5 or $10")).toBe("it costs \\$5 or \\$10");
  });

  it("escapes shell-style env-var references so they read as literal text", () => {
    expect(normalizeExplicitMathDelimiters("Set $LLM_API_KEY")).toBe("Set \\$LLM_API_KEY");
    expect(
      normalizeExplicitMathDelimiters(
        "Unresolved environment variable '$LLM_API_KEY' referenced by 'env:LLM_API_KEY'. " +
          "Set $LLM_API_KEY or $OMNIGENT_LLM_API_KEY in the environment.",
      ),
    ).toBe(
      "Unresolved environment variable '\\$LLM_API_KEY' referenced by 'env:LLM_API_KEY'. " +
        "Set \\$LLM_API_KEY or \\$OMNIGENT_LLM_API_KEY in the environment.",
    );
  });

  it("escapes braced env-var references", () => {
    expect(normalizeExplicitMathDelimiters("use ${OMNIGENT_LLM_API_KEY} here")).toBe(
      "use \\${OMNIGENT_LLM_API_KEY} here",
    );
  });

  it("escapes a single-char braced reference but not a bare single char", () => {
    // Braces disambiguate `${A}` as a variable; bare `$A …` stays inline math.
    expect(normalizeExplicitMathDelimiters("value ${A} here")).toBe("value \\${A} here");
    expect(normalizeExplicitMathDelimiters("the $A + B$ span")).toBe("the $A + B$ span");
  });

  it("does not match a SCREAMING_CASE prefix of a mixed-case token", () => {
    // `$FOO` is a prefix of `$FOOBar`; matching it would escape the opener and
    // strand the closing `$`, breaking the surrounding inline math span.
    expect(normalizeExplicitMathDelimiters("the $FOOBar$ span")).toBe("the $FOOBar$ span");
  });

  it("leaves genuine inline math intact", () => {
    expect(normalizeExplicitMathDelimiters("the value $x + y$ holds")).toBe(
      "the value $x + y$ holds",
    );
  });

  it("leaves dollars inside inline code verbatim", () => {
    expect(normalizeExplicitMathDelimiters("`$LLM_API_KEY`")).toBe("`$LLM_API_KEY`");
  });
});
