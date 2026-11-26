import { describe, expect, it } from "vitest";
import { buildDesignModePrompt } from "@/lib/designModePrompt";
import { buildDrawPrompt } from "./core/designPrompts";
import {
  clipDesignRequest,
  designSubject,
  parseDesignRequest,
  type DesignRequest,
} from "./parseDesignRequest";

describe("parseDesignRequest", () => {
  it("reads back a picked element's request (also desktop design mode's format)", () => {
    const text = buildDesignModePrompt(
      { tag: "p", id: "#blurb", text: "Your orders ship in two days." },
      "Make it red",
    );
    expect(parseDesignRequest(text)).toEqual({
      kind: "element",
      request: "Make it red",
      element: "p#blurb",
      elementText: "Your orders ship in two days.",
    });
  });

  it("reads back a drawing's note and page", () => {
    expect(
      parseDesignRequest(buildDrawPrompt("Make the heading bigger", "http://localhost:5173/a")),
    ).toEqual({ kind: "drawing", request: "Make the heading bigger", page: "localhost:5173" });
  });

  it("shows only the host of a Page an older message saved with its path", () => {
    const saved = (page: string) =>
      `Note\n\n---\n[Design Mode — marked-up screenshot of the browser preview]\nPage: ${page}\n---`;
    expect(parseDesignRequest(saved("https://example.test/invite/swordfish?code=…"))?.page).toBe(
      "example.test",
    );
    expect(parseDesignRequest(saved("file: page"))?.page).toBeUndefined();
    expect(parseDesignRequest(saved("(unknown page)"))?.page).toBeUndefined();
  });

  it("reads only the block appended last, not one quoted above", () => {
    const quoted = buildDesignModePrompt({ tag: "h1", text: "Old" }, "Earlier ask");
    const text = buildDesignModePrompt(
      { tag: "p", id: "#blurb", text: "New" },
      `${quoted}\n\nAnd now this one`,
    );
    const parsed = parseDesignRequest(text);
    expect(parsed?.element).toBe("p#blurb");
    expect(parsed?.elementText).toBe("New");
    expect(parsed?.request).toBe(`${quoted}\n\nAnd now this one`);
  });

  it("needs the generated header and its fields", () => {
    expect(
      parseDesignRequest("Hi\n\n---\n[Design Mode — modify this element?]\nSome prose\n---"),
    ).toBeNull();
    expect(
      parseDesignRequest(
        "Hi\n\n---\n[Design Mode — modify this element in the browser preview]\nProse\n---",
      ),
    ).toBeNull();
    expect(
      parseDesignRequest(
        "Hi\n\n---\n[Design Mode — marked-up screenshot of the browser preview]\nProse\n---",
      ),
    ).toBeNull();
  });

  it("keeps a component's name apart from the selector", () => {
    const parsed = parseDesignRequest(
      buildDesignModePrompt(
        { tag: "button", classes: ".primary", component: "CheckoutButton" },
        "Make it red",
      ),
    );
    expect(parsed).toMatchObject({ element: "button.primary", component: "CheckoutButton" });
    expect(designSubject(parsed!)).toBe("<CheckoutButton> button.primary");
  });

  it("keeps an element's accessible label and role", () => {
    const parsed = parseDesignRequest(
      buildDesignModePrompt(
        { tag: "button", id: "#close", ariaLabel: "Close dialog", role: "button" },
        "Bigger",
      ),
    );
    expect(parsed).toMatchObject({ ariaLabel: "Close dialog", role: "button" });
    expect(designSubject(parsed!)).toBe('button#close aria-label "Close dialog" role button');
  });

  it("names an element's page when its block does", () => {
    const text = buildDesignModePrompt({ tag: "p" }, "Red").replace(
      /\n---$/,
      "\nPage: https://example.test/orders\n---",
    );
    const parsed = parseDesignRequest(text);
    expect(parsed?.page).toBe("example.test");
    expect(designSubject(parsed!)).toBe("p on example.test");
  });

  it("notes when an element's block says no screenshot was attached", () => {
    const prompt = buildDesignModePrompt({ tag: "p" }, "Red");
    expect(parseDesignRequest(prompt)?.noScreenshot).toBeUndefined();
    const without = prompt.replace(
      /\n---$/,
      "\nScreenshot: none attached (it couldn't be taken)\n---",
    );
    expect(parseDesignRequest(without)?.noScreenshot).toBe(true);
  });

  it("parses many repeated headers in linear time", () => {
    const header = "\n\n---\n[Design Mode — modify this element in the browser preview]\n";
    const text = header.repeat(20_000);
    // A quadratic parse of this would outlast the test's timeout.
    expect(parseDesignRequest(text)).toBeNull();
    expect(parseDesignRequest(`${text}x\n---`)).toBeNull();
  });

  it("reads an element whose tag wasn't known as just its selector", () => {
    const text = buildDesignModePrompt({ testId: "x" }, "Red");
    expect(parseDesignRequest(text)).toMatchObject({ element: '[data-testid="x"]' });
    expect(parseDesignRequest(text)?.component).toBeUndefined();
  });

  it("leaves ordinary messages alone", () => {
    expect(parseDesignRequest("Please fix the build")).toBeNull();
    expect(parseDesignRequest("Intro\n\n---\nnot a design block\n---")).toBeNull();
  });
});

describe("clipDesignRequest", () => {
  it("bounds the request and element text together", () => {
    const request: DesignRequest = {
      kind: "element",
      request: "a".repeat(8),
      element: "p",
      elementText: "b".repeat(8),
    };
    const clipped = clipDesignRequest(request, 10);
    expect(clipped.request).toBe("a".repeat(8));
    expect(clipped.elementText).toBe("b…");
    expect(clipped.element).toBe("p");
  });

  it("cuts on whole characters", () => {
    // A character outside the Basic Multilingual Plane takes two UTF-16 code units.
    const wide = String.fromCodePoint(0x1d4b6);
    const request: DesignRequest = { kind: "element", request: wide.repeat(3) };
    expect(clipDesignRequest(request, 2).request).toBe(wide.repeat(2));
  });

  it("counts an emoji in the request as one character of the budget", () => {
    const request: DesignRequest = {
      kind: "element",
      request: "😀".repeat(4),
      element: "p",
      elementText: "b".repeat(8),
    };
    expect(clipDesignRequest(request, 10).elementText).toBe(`${"b".repeat(5)}…`);
  });

  it("marks element text cut to nothing, and keeps a label that matched it hidden", () => {
    const request: DesignRequest = {
      kind: "element",
      request: "a".repeat(10),
      element: "button",
      elementText: "Buy now",
      ariaLabel: "Buy now",
    };
    const clipped = clipDesignRequest(request, 10);
    expect(clipped.elementText).toBe("…");
    expect(designSubject(clipped)).toBe('button "…"');
    expect(clipDesignRequest({ ...request, request: "a" }, 10)).toEqual({
      ...request,
      request: "a",
    });
  });
});
