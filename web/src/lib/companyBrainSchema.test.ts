import { describe, expect, it } from "vitest";
import { BrainDocumentV1Schema } from "./companyBrainSchema";

const valid = {
  schema_version: "brain-document.v1",
  provider: "notion",
  connection_id: "connection-1",
  external_resource_id: "page-1",
  stable_path: "sources/notion/page-1.md",
  title: "Policy",
  markdown: "# Policy\n",
  canonical_source_url: "https://www.notion.so/page-1",
  source_created_at: null,
  source_modified_at: "2026-08-26T12:00:00Z",
  content_sha256: "a".repeat(64),
  raw_object_reference: ".raw/notion/page-1.json",
  raw_sha256: "b".repeat(64),
  transform_schema_version: "notion-page.v1",
  deletion_state: "active",
  visibility_class: "org-shared",
};

describe("BrainDocumentV1Schema", () => {
  it("accepts the locked organization-shared contract", () => {
    expect(BrainDocumentV1Schema.parse(valid).visibility_class).toBe("org-shared");
  });

  it("rejects private visibility and secret-shaped extras", () => {
    expect(() => BrainDocumentV1Schema.parse({ ...valid, visibility_class: "private" })).toThrow();
    expect(() => BrainDocumentV1Schema.parse({ ...valid, access_token: "secret" })).toThrow();
  });

  it("rejects traversal and provider-mismatched publication paths", () => {
    expect(() =>
      BrainDocumentV1Schema.parse({
        ...valid,
        raw_object_reference: ".raw/notion/../../token.json",
      }),
    ).toThrow();
    expect(() =>
      BrainDocumentV1Schema.parse({ ...valid, stable_path: "sources/slack/page-1.md" }),
    ).toThrow();
  });
});
