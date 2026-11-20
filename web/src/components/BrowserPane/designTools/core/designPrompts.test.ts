import { describe, expect, it } from "vitest";
import { buildDrawPrompt, pageAddress } from "./designPrompts";

describe("buildDrawPrompt", () => {
  it("leads with the note, or a default request", () => {
    expect(buildDrawPrompt("Make this red", "http://localhost:5173/")).toMatch(
      /^Make this red\n\n---\n\[Design Mode — marked-up screenshot of the browser preview\]\nPage: http:\/\/localhost:5173\n/,
    );
    expect(buildDrawPrompt("  ", "http://example.test/")).toMatch(/^Make the changes I marked/);
  });

  it("names the page's origin, never what its address was handed", () => {
    const text = buildDrawPrompt("Note", "https://app.example.com/a?session=s3cr3t#id_token=t0k3n");
    expect(text).toContain("Page: https://app.example.com\n");
    expect(text).not.toMatch(/s3cr3t|t0k3n|session/);
  });
});

describe("pageAddress", () => {
  it("is the page's origin only", () => {
    const signedIn = new URL(
      "https://app.example.com/orders/12?token=abc&tab=1&tab=2#access_token=x",
    );
    signedIn.username = "user";
    signedIn.password = "pw";
    expect(pageAddress(signedIn.href)).toBe("https://app.example.com");
    expect(pageAddress("http://localhost:5173/#/orders/12")).toBe("http://localhost:5173");
    // A credential shaped like a route word goes with the rest of the path and query.
    expect(pageAddress("https://example.test/r/swordfish")).toBe("https://example.test");
    expect(pageAddress("https://example.test/reset-2/swordfish?swordfish")).toBe(
      "https://example.test",
    );
    expect(pageAddress("http://localhost:5173/docs/getting-started/index.html")).toBe(
      "http://localhost:5173",
    );
  });

  it("names only the kind of a page that isn't on the web", () => {
    expect(pageAddress("not a url")).toBe("(unknown page)");
    expect(pageAddress("file:///Users/me/secret.html")).toBe("file: page");
    expect(pageAddress("about:blank")).toBe("about: page");
  });
});
