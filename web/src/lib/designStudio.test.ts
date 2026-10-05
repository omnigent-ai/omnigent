import { afterEach, describe, expect, it } from "vitest";
import {
  DECK_SLUG_MAX,
  deckPreviewState,
  deckSlug,
  designDeckPath,
  designPath,
  firstDesignMessage,
  readDesignDefaults,
  readStudioParams,
  rememberDesignDefaults,
  studioHref,
} from "./designStudio";

afterEach(() => localStorage.clear());

describe("deckSlug", () => {
  it("kebab-cases the prompt's first words", () => {
    expect(deckSlug("Pitch deck from my notes!", [])).toBe("pitch-deck-from-my-notes");
    expect(deckSlug("  Q3 Review: Café & résumé  ", [])).toBe("q3-review-cafe-resume");
  });

  it("keeps whole words up to the limit", () => {
    const slug = deckSlug(
      "Weekly status update for the platform team and everyone else on the floor",
      [],
    );
    expect(slug).toBe("weekly-status-update-for-the-platform");
    expect(slug.length).toBeLessThanOrEqual(DECK_SLUG_MAX);
  });

  it("cuts a single long word at the limit", () => {
    expect(deckSlug("a".repeat(60), [])).toBe("a".repeat(DECK_SLUG_MAX));
  });

  it("falls back to deck when nothing is left", () => {
    expect(deckSlug("!!! ???", [])).toBe("deck");
  });

  it("adds -2, -3 when the name is taken", () => {
    expect(deckSlug("Product launch", ["product-launch"])).toBe("product-launch-2");
    expect(deckSlug("Product launch", ["product-launch", "product-launch-2"])).toBe(
      "product-launch-3",
    );
  });
});

describe("first message", () => {
  it("puts the deck under decks/ and the wireframe under wireframes/", () => {
    expect(designDeckPath("pitch")).toBe("decks/pitch.slides.html");
    expect(designPath("pitch", "deck")).toBe("decks/pitch.slides.html");
    expect(designPath("sign-up", "wireframe")).toBe("wireframes/sign-up.wireframe.html");
  });

  it("names the wireframes skill for a wireframe", () => {
    expect(firstDesignMessage("Sign-up flow", "wireframes/sign-up-flow.wireframe.html")).toBe(
      "Sign-up flow\n\nUse the wireframes skill. Write the wireframe to " +
        "`wireframes/sign-up-flow.wireframe.html`. Write a complete document with only the " +
        "first screen first, then add one complete screen per edit.",
    );
  });

  it("appends the studio instructions to the prompt", () => {
    expect(firstDesignMessage("  Pitch deck  ", "decks/pitch.slides.html")).toBe(
      "Pitch deck\n\nUse the slide-decks skill. Write the deck to `decks/pitch.slides.html`. " +
        "Write a complete document with only the title slide first, then add one complete " +
        "slide per edit.",
    );
  });

  it("adds the design-system instruction when one is chosen", () => {
    const message = firstDesignMessage("Pitch", "decks/pitch.slides.html", {
      path: "/brand",
      kind: "skill",
      name: "Brand",
    });
    expect(message).toMatch(
      /slide per edit\.\n\nFollow the design system at `\/brand` \(`skill`\)/,
    );
    expect(message.endsWith("Read its SKILL.md first.")).toBe(true);
  });
});

describe("studio URL", () => {
  it("needs both a session and a file", () => {
    expect(readStudioParams(new URLSearchParams("session=a"))).toBeNull();
    expect(readStudioParams(new URLSearchParams("file=x"))).toBeNull();
  });

  it("reads the view, defaulting to preview", () => {
    expect(readStudioParams(new URLSearchParams("session=a&file=d.slides.html"))).toEqual({
      sessionId: "a",
      path: "d.slides.html",
      view: "preview",
    });
    expect(readStudioParams(new URLSearchParams("session=a&file=d&view=full"))?.view).toBe("full");
    expect(readStudioParams(new URLSearchParams("session=a&file=d&view=chat"))?.view).toBe("chat");
    expect(readStudioParams(new URLSearchParams("session=a&file=d&view=bogus"))?.view).toBe(
      "preview",
    );
  });

  it("builds links that omit the default view", () => {
    expect(studioHref("a", "decks/p.slides.html")).toBe(
      "/design?session=a&file=decks%2Fp.slides.html",
    );
    expect(studioHref("a", "d", "full")).toBe("/design?session=a&file=d&view=full");
  });
});

describe("design defaults", () => {
  it("is empty until something is remembered", () => {
    expect(readDesignDefaults()).toEqual({});
  });

  it("remembers the agent, host, and folder per host", () => {
    rememberDesignDefaults("agent_1", "host_a", "/work/a");
    rememberDesignDefaults("agent_2", "host_b", "/work/b");
    expect(readDesignDefaults()).toEqual({
      agentId: "agent_2",
      hostId: "host_b",
      folders: { host_a: "/work/a", host_b: "/work/b" },
    });
  });

  it("treats broken storage as empty", () => {
    localStorage.setItem("omnigent.design.defaults", "{not json");
    expect(readDesignDefaults()).toEqual({});
    localStorage.setItem("omnigent.design.defaults", "[1]");
    expect(readDesignDefaults()).toEqual({});
  });
});

describe("deckPreviewState", () => {
  it("shows the deck once it exists", () => {
    expect(deckPreviewState({ file: "ok", turnEnded: true })).toBe("deck");
  });

  it("waits for a missing deck until the turn ends", () => {
    expect(deckPreviewState({ file: "missing", turnEnded: false })).toBe("waiting");
    expect(deckPreviewState({ file: "missing", turnEnded: true })).toBe("not-written");
  });

  it("passes loading and errors through", () => {
    expect(deckPreviewState({ file: "loading", turnEnded: false })).toBe("loading");
    expect(deckPreviewState({ file: "error", turnEnded: true })).toBe("error");
  });
});
