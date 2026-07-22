import { describe, expect, it } from "vitest";
import { artifactEntriesFromFiles } from "./ArtifactsPanel";

describe("artifactEntriesFromFiles", () => {
  it("keeps only canonical standalone and directory HTML entries", () => {
    expect(
      artifactEntriesFromFiles([
        {
          path: "artifacts/overview.html",
          name: "overview.html",
          type: "file",
          bytes: 100,
          modified_at: 1,
        },
        {
          path: "artifacts/revenue/index.html",
          name: "index.html",
          type: "file",
          bytes: 200,
          modified_at: 2,
        },
        {
          path: "artifacts/revenue/assets/help.html",
          name: "help.html",
          type: "file",
          bytes: 50,
          modified_at: 3,
        },
        {
          path: "docs/example.html",
          name: "example.html",
          type: "file",
          bytes: 50,
          modified_at: 4,
        },
      ]),
    ).toEqual([
      { entryPath: "artifacts/overview.html", title: "Overview", modifiedAt: 1 },
      { entryPath: "artifacts/revenue/index.html", title: "Revenue", modifiedAt: 2 },
    ]);
  });
});
