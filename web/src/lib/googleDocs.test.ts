import { describe, expect, it } from "vitest";
import urlCases from "./__fixtures__/googleDocsUrls.json";
import { classifyGoogleDocsUrl } from "./googleDocs";

describe("Google Docs URL contract", () => {
  it.each(urlCases)("$name", ({ url, kind, canonicalUrl }) => {
    const result = classifyGoogleDocsUrl(url);
    expect(result.kind).toBe(kind);
    if (result.kind === "embed") expect(result.reference.canonicalUrl).toBe(canonicalUrl);
    if (result.kind === "external") expect(result.originalUrl).toBe(url);
  });

  it("bounds canonical URLs and preserves access-key/deep-link targets before tab validation", () => {
    expect(
      classifyGoogleDocsUrl("https://docs.google.com/document/d/" + "x".repeat(2048) + "/edit")
        .kind,
    ).toBe("invalid");
    for (const suffix of ["?resourcekey=secret&tab=invalid", "?tab=invalid#heading=h.one"]) {
      const originalUrl = " https://docs.google.com/document/d/example/edit" + suffix + " ";
      expect(classifyGoogleDocsUrl(originalUrl)).toEqual({ kind: "external", originalUrl });
    }
  });
});
