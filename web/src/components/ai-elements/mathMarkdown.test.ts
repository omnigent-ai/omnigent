import { describe, expect, it } from "vitest";
import { normalizeExplicitMathDelimiters } from "./mathMarkdown";

describe("normalizeExplicitMathDelimiters — dollar handling", () => {
  it("converts TeX delimiters to double dollars outside code", () => {
    expect(normalizeExplicitMathDelimiters("area is \\(a^2\\)")).toBe("area is $$a^2$$");
    expect(normalizeExplicitMathDelimiters("\\[a+b\\]")).toBe("$$a+b$$");
  });

  it("leaves currency verbatim — a lone dollar is prose, not a delimiter", () => {
    expect(normalizeExplicitMathDelimiters("it costs $5 or $10")).toBe("it costs $5 or $10");
    // The reported bug: paired-up rate figures turned the prose between them
    // into math. `$/PR` is the shape no escaping heuristic caught — a `$`
    // before a slash. Nothing here may be escaped or rewritten.
    const rates = "$/PR versus $/session, a 60% saving";
    expect(normalizeExplicitMathDelimiters(rates)).toBe(rates);
  });

  it("leaves shell-style env-var references verbatim", () => {
    expect(normalizeExplicitMathDelimiters("Set $LLM_API_KEY")).toBe("Set $LLM_API_KEY");
    const unresolved =
      "Unresolved environment variable '$LLM_API_KEY' referenced by 'env:LLM_API_KEY'. " +
      "Set $LLM_API_KEY or $OMNIGENT_LLM_API_KEY in the environment.";
    expect(normalizeExplicitMathDelimiters(unresolved)).toBe(unresolved);
  });

  it("leaves braced env-var references verbatim", () => {
    expect(normalizeExplicitMathDelimiters("use ${OMNIGENT_LLM_API_KEY} here")).toBe(
      "use ${OMNIGENT_LLM_API_KEY} here",
    );
    expect(normalizeExplicitMathDelimiters("value ${A} here")).toBe("value ${A} here");
  });

  it("leaves a single-dollar span verbatim, whatever it contains", () => {
    // None of these are math delimiters any more, so the text passes through
    // untouched and renders as the literal characters the agent wrote.
    expect(normalizeExplicitMathDelimiters("the $A + B$ span")).toBe("the $A + B$ span");
    expect(normalizeExplicitMathDelimiters("the $FOOBar$ span")).toBe("the $FOOBar$ span");
    expect(normalizeExplicitMathDelimiters("the value $x + y$ holds")).toBe(
      "the value $x + y$ holds",
    );
  });

  it("still converts delimiters after prose dollars", () => {
    // A lone `$` must not flip the math-span state, or the conversion below it
    // would be suppressed for the rest of the message.
    expect(normalizeExplicitMathDelimiters("costs $5, so \\(x + 1\\)")).toBe(
      "costs $5, so $$x + 1$$",
    );
  });

  it("leaves dollars inside inline code verbatim", () => {
    expect(normalizeExplicitMathDelimiters("`$LLM_API_KEY`")).toBe("`$LLM_API_KEY`");
  });
});
