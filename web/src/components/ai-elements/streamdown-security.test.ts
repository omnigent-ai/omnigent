import { describe, expect, it } from "vitest";
import { githubAttachmentAssetId } from "./streamdown-security";

describe("githubAttachmentAssetId", () => {
  const id = "381e47c8-e3f1-4c3e-9b3d-aac5cfb3557b";
  const asset = `https://github.com/user-attachments/assets/${id}`;

  it("reads the id of an attachment image, in lowercase", () => {
    expect(githubAttachmentAssetId(asset)).toBe(id);
    expect(githubAttachmentAssetId(asset.replace(id, id.toUpperCase()))).toBe(id);
  });

  it.each([
    ["nothing", undefined],
    ["null", null],
    ["another host", asset.replace("github.com", "example.com")],
    ["a lookalike host", asset.replace("github.com", "github.com.example.com")],
    ["a query", `${asset}?raw=1`],
    ["a longer path", `${asset}/download`],
    ["an id that isn't a UUID", "https://github.com/user-attachments/assets/deadbeef"],
  ])("is null for %s", (_case, src) => {
    expect(githubAttachmentAssetId(src)).toBeNull();
  });
});
