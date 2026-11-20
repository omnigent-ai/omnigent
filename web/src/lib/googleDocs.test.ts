import { describe, expect, it } from "vitest";

import urlCases from "./__fixtures__/googleDocsUrls.json";
import {
  buildGoogleDocsLabelPatch,
  classifyGoogleDocsUrl,
  GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX,
  GOOGLE_DOCS_SELECTION_LABEL,
  readGoogleDocsLabels,
} from "./googleDocs";

const EDITOR = "https://docs.google.com/document/d/";
const DOC_A = EDITOR + "A/edit?tab=t.0";
const DOC_B = EDITOR + "B/edit?tab=t.two";
const DOC_C = EDITOR + "C/edit";
const ENTRY_A = GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + "A";
const ENTRY_B = GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + "B";
const ENTRY_C = GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + "C";

describe("classifyGoogleDocsUrl", () => {
  it.each(urlCases)("$name", ({ url, kind, canonicalUrl }) => {
    const result = classifyGoogleDocsUrl(url);
    if (kind === "embed") {
      expect(result).toEqual({
        kind,
        reference: {
          documentId: "Doc_1-abc",
          tabId: canonicalUrl?.split("?tab=")[1] ?? null,
          canonicalUrl,
        },
      });
    } else if (kind === "external") {
      expect(result).toEqual({ kind, originalUrl: url });
    } else {
      expect(result.kind).toBe("invalid");
    }
  });

  it("keeps the input of an external link unchanged", () => {
    const url = " HTTPS://DOCS.GOOGLE.COM/document/d/A/edit?tab=t.0#heading=h.intro ";
    expect(classifyGoogleDocsUrl(url)).toEqual({ kind: "external", originalUrl: url });
    expect(() => buildGoogleDocsLabelPatch({}, url)).toThrow("must be opened in Google Docs");
  });
});

describe("Google Docs labels", () => {
  it("checks both store bounds before building a patch", () => {
    const documentId = "a".repeat(128 - GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX.length);
    const url = (EDITOR + documentId + "/edit?tab=t.").padEnd(256, "b");

    expect(buildGoogleDocsLabelPatch({}, url)).toEqual({
      [GOOGLE_DOCS_SELECTION_LABEL]: url,
      [GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + documentId]: url,
    });
    expect(() => buildGoogleDocsLabelPatch({}, url + "b")).toThrow("URL exceeds 256");
    expect(() => buildGoogleDocsLabelPatch({}, EDITOR + documentId + "a/edit")).toThrow(
      "label key exceeds 128",
    );
  });

  it("reads a legacy selection and adds its entry on explicit save", () => {
    const legacy = EDITOR + "A/edit?usp=sharing#tab=t.0";
    const labels = { [GOOGLE_DOCS_SELECTION_LABEL]: legacy };
    const reference = { documentId: "A", tabId: "t.0", canonicalUrl: DOC_A };

    expect(readGoogleDocsLabels(labels)).toEqual({
      selected: reference,
      documents: new Map([["A", reference]]),
    });
    expect(labels).toEqual({ [GOOGLE_DOCS_SELECTION_LABEL]: legacy });
    expect(buildGoogleDocsLabelPatch(labels, DOC_A)).toEqual({
      [GOOGLE_DOCS_SELECTION_LABEL]: DOC_A,
      [ENTRY_A]: DOC_A,
    });
    expect(buildGoogleDocsLabelPatch(labels, null)).toEqual({ [GOOGLE_DOCS_SELECTION_LABEL]: "" });
  });

  it("changes or unlinks only the selected entry", () => {
    const labels = {
      [GOOGLE_DOCS_SELECTION_LABEL]: DOC_A,
      [ENTRY_A]: DOC_A,
      [ENTRY_C]: DOC_C,
      "other.label": "retained",
    };
    const change = buildGoogleDocsLabelPatch(labels, DOC_B);
    expect(change).toEqual({
      [GOOGLE_DOCS_SELECTION_LABEL]: DOC_B,
      [ENTRY_A]: "",
      [ENTRY_B]: DOC_B,
    });
    const changed = { ...labels, ...change };
    const clear = buildGoogleDocsLabelPatch(changed, null);
    expect(clear).toEqual({ [GOOGLE_DOCS_SELECTION_LABEL]: "", [ENTRY_B]: "" });
    expect(readGoogleDocsLabels({ ...changed, ...clear })).toEqual({
      selected: null,
      documents: new Map([["C", { documentId: "C", tabId: null, canonicalUrl: DOC_C }]]),
    });
  });

  it("uses the selected tab and ignores mismatched or invalid entries", () => {
    const url = EDITOR + "A/edit?tab=t.next";
    const labels = {
      [GOOGLE_DOCS_SELECTION_LABEL]: url,
      [ENTRY_A]: DOC_A,
      [ENTRY_B]: DOC_C,
      [ENTRY_C]: DOC_C + "#heading=h.section",
    };
    const reference = { documentId: "A", tabId: "t.next", canonicalUrl: url };
    expect(readGoogleDocsLabels(labels)).toEqual({
      selected: reference,
      documents: new Map([["A", reference]]),
    });
    expect(buildGoogleDocsLabelPatch(labels, DOC_A)).toEqual({
      [GOOGLE_DOCS_SELECTION_LABEL]: DOC_A,
      [ENTRY_A]: DOC_A,
    });
  });
});
